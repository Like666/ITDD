"""
Implementation of "Attention is All You Need"
"""

import torch
import torch.nn as nn
import numpy as np

import onmt
from onmt.modules.position_ffn import PositionwiseFeedForward

MAX_SIZE = 5000


class TransformerDecoderLayer(nn.Module):
    """
    Args:
      d_model (int): the dimension of keys/values/queries in
                       MultiHeadedAttention, also the input size of
                       the first-layer of the PositionwiseFeedForward.
      heads (int): the number of heads for MultiHeadedAttention.
      d_ff (int): the second-layer of the PositionwiseFeedForward.
      dropout (float): dropout probability(0-1.0).
      self_attn_type (string): type of self-attention scaled-dot, average
    """

    def __init__(self, d_model, heads, d_ff, dropout,
                 self_attn_type="scaled-dot"):
        super(TransformerDecoderLayer, self).__init__()

        self.self_attn_type = self_attn_type

        if self_attn_type == "scaled-dot":
            self.self_attn = onmt.modules.MultiHeadedAttention(
                heads, d_model, dropout=dropout)
        elif self_attn_type == "average":
            self.self_attn = onmt.modules.AverageAttention(
                d_model, dropout=dropout)

        self.knowledge_attn = onmt.modules.MultiHeadedAttention(
            heads, d_model, dropout=dropout)
        self.history_attn = onmt.modules.MultiHeadedAttention(
            heads, d_model, dropout=dropout)
        self.feed_forward = PositionwiseFeedForward(d_model, d_ff, dropout)
        self.layer_norm_1 = nn.LayerNorm(d_model, eps=1e-6)
        self.layer_norm_2 = nn.LayerNorm(d_model, eps=1e-6)
        self.layer_norm_3 = nn.LayerNorm(d_model, eps=1e-6)
        self.dropout = dropout
        self.drop = nn.Dropout(dropout)
        mask = self._get_attn_subsequent_mask(MAX_SIZE)
        # Register self.mask as a buffer in TransformerDecoderLayer, so
        # it gets TransformerDecoderLayer's cuda behavior automatically.
        self.register_buffer('mask', mask)

    def forward(self, inputs, src_bank, knl_bank, src_pad_mask, knl_pad_mask, tgt_pad_mask,
                layer_cache=None, step=None):
        """
        Args:
            inputs (`FloatTensor`): `[batch_size x 1 x model_dim]`
            memory_bank (`FloatTensor`): `[batch_size x src_len x model_dim]`
            src_pad_mask (`LongTensor`): `[batch_size x 1 x src_len]`
            tgt_pad_mask (`LongTensor`): `[batch_size x 1 x 1]`

        Returns:
            (`FloatTensor`, `FloatTensor`):

            * output `[batch_size x 1 x model_dim]`
            * attn `[batch_size x 1 x src_len]`

        """
        dec_mask = None
        if step is None:
            dec_mask = torch.gt(tgt_pad_mask +
                                self.mask[:, :tgt_pad_mask.size(-1),
                                          :tgt_pad_mask.size(-1)], 0)

        input_norm = self.layer_norm_1(inputs)

        if self.self_attn_type == "scaled-dot":
            query, attn = self.self_attn(input_norm, input_norm, input_norm,
                                         mask=dec_mask,
                                         layer_cache=layer_cache,
                                         type="self")
        elif self.self_attn_type == "average":
            query, attn = self.self_attn(input_norm, mask=dec_mask,
                                         layer_cache=layer_cache, step=step)

        query = self.drop(query) + inputs

        query_norm = self.layer_norm_2(query)
        mid, attn = self.knowledge_attn(knl_bank, knl_bank, query_norm,
                                        mask=knl_pad_mask,
                                        layer_cache=layer_cache,
                                        type="knl")
        knl_out = self.drop(mid) + query
        knl_out_norm = self.layer_norm_3(knl_out)

        out, attn = self.history_attn(src_bank, src_bank, knl_out_norm,
                                      mask=src_pad_mask,
                                      layer_cache=layer_cache,
                                      type="src")

        output = self.feed_forward(self.drop(out) + knl_out)

        return output, attn

    def _get_attn_subsequent_mask(self, size):
        """
        Get an attention mask to avoid using the subsequent info.

        Args:
            size: int

        Returns:
            (`LongTensor`):

            * subsequent_mask `[1 x size x size]`
        """
        attn_shape = (1, size, size)
        subsequent_mask = np.triu(np.ones(attn_shape), k=1).astype('uint8')
        subsequent_mask = torch.from_numpy(subsequent_mask)
        return subsequent_mask


class TransformerDecoder(nn.Module):
    """
    The Transformer decoder from "Attention is All You Need".


    .. mermaid::

       graph BT
          A[input]
          B[multi-head self-attn]
          BB[multi-head src-attn]
          C[feed forward]
          O[output]
          A --> B
          B --> BB
          BB --> C
          C --> O


    Args:
       num_layers (int): number of encoder layers.
       d_model (int): size of the model
       heads (int): number of heads
       d_ff (int): size of the inner FF layer
       dropout (float): dropout parameters
       embeddings (:obj:`onmt.modules.Embeddings`):
          embeddings to use, should have positional encodings
       attn_type (str): if using a seperate copy attention
    """

    def __init__(self, num_layers, d_model, heads, d_ff, attn_type,
                 copy_attn, self_attn_type, dropout, embeddings):
        super(TransformerDecoder, self).__init__()

        # Basic attributes.
        self.decoder_type = 'transformer'
        self.num_layers = num_layers
        self.embeddings = embeddings
        self.self_attn_type = self_attn_type

        # Decoder State
        self.state = {}

        # Build TransformerDecoder.
        self.transformer_layers = nn.ModuleList(
            [TransformerDecoderLayer(d_model, heads, d_ff, dropout,
             self_attn_type=self_attn_type)
             for _ in range(num_layers)])

        # TransformerDecoder has its own attention mechanism.
        # Set up a separated copy attention layer, if needed.
        self._copy = False
        if copy_attn:
            self.copy_attn = onmt.modules.GlobalAttention(
                d_model, attn_type=attn_type)
            self._copy = True
        self.layer_norm = nn.LayerNorm(d_model, eps=1e-6)

    def init_state(self, src, knl, memory_bank, enc_hidden):
        """ Init decoder state """
        self.state["src"] = src
        self.state["knl"] = knl
        self.state["cache"] = None

    def map_state(self, fn):
        def _recursive_map(struct, batch_dim=0):
            for k, v in struct.items():
                if v is not None:
                    if isinstance(v, dict):
                        _recursive_map(v)
                    else:
                        struct[k] = fn(v, batch_dim)

        self.state["src"] = fn(self.state["src"], 1)
        self.state["knl"] = fn(self.state["knl"], 1)
        if self.state["cache"] is not None:
            _recursive_map(self.state["cache"])

    def detach_state(self):
        self.state["src"] = self.state["src"].detach()
        self.state["knl"] = self.state["knl"].detach()

    def forward(self, tgt, src_bank, knl_bank, memory_lengths=None, step=None):
        """
        See :obj:`onmt.modules.RNNDecoderBase.forward()`
        """
        if step == 0:
            self._init_cache(src_bank, self.num_layers, self.self_attn_type)

        src = self.state["src"]
        knl = self.state["knl"]
        src_words = src[:, :, 0].transpose(0, 1)
        knl_words = knl[:, :, 0].transpose(0, 1)
        tgt_words = tgt[:, :, 0].transpose(0, 1)
        knl_batch, knl_len = knl_words.size()
        src_batch, src_len = src_words.size()
        tgt_batch, tgt_len = tgt_words.size()

        # Initialize return variables.
        dec_outs = []
        attns = {"std": []}
        if self._copy:
            attns["copy"] = []

        # Run the forward pass of the TransformerDecoder.
        emb = self.embeddings(tgt, step=step)
        assert emb.dim() == 3  # len x batch x embedding_dim

        output = emb.transpose(0, 1).contiguous()
        src_memory_bank = src_bank.transpose(0, 1).contiguous()
        knl_memory_bank = knl_bank.transpose(0, 1).contiguous()

        pad_idx = self.embeddings.word_padding_idx
        knl_pad_mask = knl_words.data.eq(pad_idx).unsqueeze(1)  # [B, 1, T_knl]
        src_pad_mask = src_words.data.eq(pad_idx).unsqueeze(1)  # [B, 1, T_src]
        tgt_pad_mask = tgt_words.data.eq(pad_idx).unsqueeze(1)  # [B, 1, T_tgt]

        for i in range(self.num_layers):
            output, attn = self.transformer_layers[i](
                output,
                src_memory_bank,
                knl_memory_bank,
                src_pad_mask,
                knl_pad_mask,
                tgt_pad_mask,
                layer_cache=(
                    self.state["cache"]["layer_{}".format(i)]
                    if step is not None else None),
                step=step)

        output = self.layer_norm(output)

        # Process the result and update the attentions.
        dec_outs = output.transpose(0, 1).contiguous()
        attn = attn.transpose(0, 1).contiguous()

        attns["std"] = attn
        if self._copy:
            attns["copy"] = attn

        # TODO change the way attns is returned dict => list or tuple (onnx)
        return dec_outs, attns

    def _init_cache(self, memory_bank, num_layers, self_attn_type):
        self.state["cache"] = {}
        batch_size = memory_bank.size(1)
        depth = memory_bank.size(-1)

        for l in range(num_layers):
            layer_cache = {
                "src_memory_keys": None,
                "src_memory_values": None,
                "knl_memory_keys": None,
                "knl_memory_values": None
            }
            if self_attn_type == "scaled-dot":
                layer_cache["self_keys"] = None
                layer_cache["self_values"] = None
            elif self_attn_type == "average":
                layer_cache["prev_g"] = torch.zeros((batch_size, 1, depth))
            else:
                layer_cache["self_keys"] = None
                layer_cache["self_values"] = None
            self.state["cache"]["layer_{}".format(l)] = layer_cache
