from typing import Optional, Tuple

import torch
import torch.nn as nn
from torch.nn.functional import normalize

from dev_misc import BT, FT, LT, get_zeros
from dev_misc.devlib.named_tensor import NoName
from sound_law.model.lstm_state import LstmStatesByLayers, LstmStateTuple

LstmOutputsByLayers = Tuple[FT, LstmStatesByLayers]
LstmOutputTuple = Tuple[FT, LstmStateTuple]


class MultiLayerLSTMCell(nn.Module):
    """An LSTM cell with multiple layers."""

    def __init__(self,
                 input_size: int,
                 hidden_size: int,
                 num_layers: int,
                 dropout: float = 0.0):
        super().__init__()

        self.input_size = input_size
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.drop = nn.Dropout(dropout)

        cells = [nn.LSTMCell(input_size, hidden_size)]
        for _ in range(self.num_layers - 1):
            cells.append(nn.LSTMCell(hidden_size, hidden_size))
        self.cells = nn.ModuleList(cells)

    def forward(self, input_: FT, state: LstmStatesByLayers, state_direction: Optional[str] = None) -> LstmOutputsByLayers:
        assert state.num_layers == self.num_layers

        new_states = list()
        for i in range(self.num_layers):
            # Note that the last layer doesn't use dropout.
            input_ = self.drop(input_)
            with NoName(input_):
                new_state = self.cells[i](input_, state.get_layer(i, state_direction))
            new_states.append(new_state)
            input_ = new_state[0].refine_names('batch', ...)
        return input_, LstmStatesByLayers(new_states)

    def extra_repr(self):
        return '%d, %d, num_layers=%d' % (self.input_size, self.hidden_size, self.num_layers)


class SharedEmbedding(nn.Embedding):
    """Shared input and output embedding."""

    def project(self, h: FT) -> FT:
        return h @ self.weight.t()


class LstmCellWithEmbedding(nn.Module):
    """An LSTM cell on top of an embedding layer."""

    def __init__(self,
                 num_embeddings: int,
                 input_size: int,
                 hidden_size: int,
                 num_layers: int,
                 dropout: float = 0.0,
                 embedding: Optional[nn.Module] = None):
        super().__init__()

        self.embedding = embedding or SharedEmbedding(num_embeddings, input_size)
        self.lstm = MultiLayerLSTMCell(input_size, hidden_size, num_layers, dropout=dropout)

    def embed(self, input_: LT) -> FT:
        return self.embedding(input_)

    def forward(self,
                input_: LT,
                init_state: Optional[LstmStatesByLayers] = None,
                state_direction: Optional[str] = None) -> LstmOutputsByLayers:
        emb = self.embed(input_)

        batch_size = input_.size('batch')
        init_state = init_state or LstmStateTuple.zero_state(self.lstm.num_layers,
                                                             batch_size,
                                                             self.lstm.hidden_size)
        output, state = self.lstm(emb, init_state, state_direction=state_direction)
        return output, state


class LstmDecoder(LstmCellWithEmbedding):
    """A decoder that unrolls the LSTM decoding procedure by steps."""

    def forward(self,
                sot_id: int,  # "start-of-token"
                init_state: LstmStatesByLayers,
                max_length: Optional[int] = None,
                target: Optional[LT] = None,
                init_state_direction: Optional[str] = None) -> FT:
        max_length = self._get_max_length(max_length, target)
        input_ = self._prepare_first_input(sot_id, init_state)
        log_probs = list()
        state = init_state
        state_direction = init_state_direction
        for l in range(max_length):
            input_, state, log_prob = self._forward_step(
                l, input_, state, target=target, state_direction=state_direction)
            # NOTE(j_luo) Only the first state uses actual `init_state_direction`.
            state_direction = None
            log_probs.append(log_prob)

        with NoName(*log_probs):
            log_probs = torch.stack(log_probs, dim=0).refine_names('pos', 'batch', 'unit')
        return log_probs

    def _get_max_length(self, max_length: Optional[int], target: Optional[LT]) -> int:
        if self.training:
            assert target is not None
            assert target.names[1] == 'batch'
            assert len(target.shape) == 2
        if max_length is None:
            max_length = target.size("pos")
        return max_length

    def _prepare_first_input(self, sot_id: int, init_state: LstmStatesByLayers):
        state = init_state
        batch_size = init_state.batch_size
        log_probs = list()
        input_ = torch.full([batch_size], sot_id, dtype=torch.long).rename('batch').to(init_state.device)
        return input_

    def _forward_step(self,
                      step: int,
                      input_: FT,
                      state: LstmStatesByLayers,
                      target: Optional[LT] = None,
                      state_direction: Optional[str] = None) -> Tuple[FT, FT, FT]:
        """Do one step of decoding. Return the input and the state for the next step, as well as the log probs."""
        output, next_state = super().forward(input_, state, state_direction=state_direction)
        logit = self.embedding.project(output)
        log_prob = logit.log_softmax(dim=-1)

        if self.training:
            next_input = target[step]
        else:
            next_input = log_prob.max(dim=-1)[1]

        return next_input, next_state, log_prob


class GlobalAttention(nn.Module):

    def __init__(self,
                 input_src_size: int,
                 input_tgt_size: int,
                 dropout: float = 0.0):
        super(GlobalAttention, self).__init__()

        self.input_src_size = input_src_size
        self.input_tgt_size = input_tgt_size

        self.Wa = nn.Parameter(torch.Tensor(input_src_size, input_tgt_size))
        self.drop = nn.Dropout(dropout)

    def forward(self,
                h_t: FT,
                h_s: FT,
                mask_src: BT) -> Tuple[FT, FT]:
        sl, bs, ds = h_s.size()
        dt = h_t.shape[-1]
        Wh_s = self.drop(h_s).reshape(sl * bs, -1).mm(self.Wa).view(sl, bs, -1)

        scores = Wh_s.matmul(self.drop(h_t).unsqueeze(dim=-1)).squeeze(dim=-1)  # sl x bs

        scores = torch.where(mask_src, scores, torch.full_like(scores, -9999.9))
        almt_distr = nn.functional.log_softmax(scores, dim=0).exp()  # sl x bs
        ctx = (almt_distr.unsqueeze(dim=-1) * h_s).sum(dim=0)  # bs x d
        almt_distr = almt_distr.t()
        return almt_distr, ctx

    def extra_repr(self):
        return 'src=%d, tgt=%d' % (self.input_src_size, self.input_tgt_size)


class LstmDecoderWithAttention(LstmDecoder):

    def __init__(self,
                 num_embeddings: int,
                 input_size: int,
                 src_hidden_size: int,
                 tgt_hidden_size: int,
                 num_layers: int,
                 dropout: float = 0.0,
                 emb_dropout: float = 0.0,
                 embedding: Optional[nn.Module] = None):
        super().__init__(num_embeddings, input_size, tgt_hidden_size, num_layers,
                         dropout=dropout, embedding=embedding)
        self.attn = GlobalAttention(src_hidden_size, tgt_hidden_size)
        self.hidden = nn.Linear(src_hidden_size + tgt_hidden_size, tgt_hidden_size)

    def forward(self,
                sot_id: int,
                src_states: FT,
                mask_src: BT,
                init_state: LstmStatesByLayers,
                max_length: Optional[int] = None,
                target: Optional[LT] = None) -> FT:
        max_length = self._get_max_length(max_length, target)
        input_ = self._prepare_first_input(sot_id, init_state)
        log_probs = list()
        for l in range(max_length):
            input_, state, log_prob = self._forward_step(l, input_, state, src_states, mask_src, target=target)
            log_probs.append(log_prob)

    def _forward_step(self,
                      step: int,
                      input_: FT,
                      state: LstmStatesByLayers,
                      src_states: FT,
                      mask_src: BT,
                      target: Optional[LT] = None):
        _, ctx = self.attn.forward(input_, src_states, mask_src)  # FIXME(j_luo) add mask here
        cat = torch.cat([input_, ctx], dim=0)
        hid_input = self.hidden(cat)
        return super()._forward_step(step, hid_input, state, target=target)


class LstmEncoder(nn.Module):

    def __init__(self,
                 num_embeddings: int,
                 input_size: int,
                 hidden_size: int,
                 num_layers: int,
                 dropout: float = 0.0,
                 bidirectional: bool = False,
                 embedding: Optional[nn.Module] = None):
        super().__init__()
        self.embedding = embedding or SharedEmbedding(num_embeddings, input_size)
        self.dropout = nn.Dropout(dropout)
        self.lstm = nn.LSTM(input_size, hidden_size, num_layers, bidirectional=bidirectional, dropout=dropout)

    def forward(self, input_: LT) -> LstmOutputTuple:
        # FIXME(j_luo) what happened to paddings?
        batch_size = input_.size('batch')
        emb = self.embedding(input_)
        with NoName(emb):
            output, state = self.lstm(emb)
        return output, LstmStateTuple(state, bidirectional=self.lstm.bidirectional)
