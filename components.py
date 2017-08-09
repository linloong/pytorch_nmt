import math
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.utils
from torch.autograd import Variable
from torch.nn import Parameter, init
from torch.nn._functions.rnn import variable_recurrent_factory, StackedRNN
from torch.nn.modules.rnn import RNNCellBase
from torch.nn.utils.rnn import PackedSequence
from torch.nn._functions.thnn import rnnFusedPointwise as fusedBackend


class LSTMCell(RNNCellBase):

    def __init__(self, input_size, hidden_size, dropout=0.):
        super(LSTMCell, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.dropout = dropout

        self.W_i = Parameter(torch.Tensor(hidden_size, input_size))
        self.U_i = Parameter(torch.Tensor(hidden_size, hidden_size))
        self.b_i = Parameter(torch.Tensor(hidden_size))

        self.W_f = Parameter(torch.Tensor(hidden_size, input_size))
        self.U_f = Parameter(torch.Tensor(hidden_size, hidden_size))
        self.b_f = Parameter(torch.Tensor(hidden_size))

        self.W_c = Parameter(torch.Tensor(hidden_size, input_size))
        self.U_c = Parameter(torch.Tensor(hidden_size, hidden_size))
        self.b_c = Parameter(torch.Tensor(hidden_size))

        self.W_o = Parameter(torch.Tensor(hidden_size, input_size))
        self.U_o = Parameter(torch.Tensor(hidden_size, hidden_size))
        self.b_o = Parameter(torch.Tensor(hidden_size))

        self._input_dropout_mask = self._h_dropout_mask = None

        self.reset_parameters()

    def reset_parameters(self):
        init.orthogonal(self.W_i)
        init.orthogonal(self.U_i)
        init.orthogonal(self.W_f)
        init.orthogonal(self.U_f)
        init.orthogonal(self.W_c)
        init.orthogonal(self.U_c)
        init.orthogonal(self.W_o)
        init.orthogonal(self.U_o)
        self.b_f.data.fill_(1.)
        self.b_i.data.zero_()
        self.b_c.data.zero_()
        self.b_o.data.zero_()

    def set_dropout_masks(self, batch_size):
        if self.dropout:
            if self.training:
                self._input_dropout_mask = Variable(torch.bernoulli(
                    torch.Tensor(4, batch_size, self.input_size).fill_(1 - self.dropout)), requires_grad=False)
                self._h_dropout_mask = Variable(torch.bernoulli(
                    torch.Tensor(4, batch_size, self.hidden_size).fill_(1 - self.dropout)), requires_grad=False)

                if torch.cuda.is_available():
                    self._input_dropout_mask = self._input_dropout_mask.cuda()
                    self._h_dropout_mask = self._h_dropout_mask.cuda()
            else:
                self._input_dropout_mask = self._h_dropout_mask = [1. - self.dropout] * 4
        else:
            self._input_dropout_mask = self._h_dropout_mask = [1.] * 4

    def forward(self, input, hidden_state):
        h_tm1, c_tm1 = hidden_state

        if self._input_dropout_mask is None:
            self.set_dropout_masks(input.size(0))

        def get_mask_slice(mask, idx):
            if isinstance(mask, list): return mask[idx]
            else: return mask[idx][-input.size(0):]

        hi_t = F.linear(h_tm1 * get_mask_slice(self._h_dropout_mask, 0), self.U_i)
        hf_t = F.linear(h_tm1 * get_mask_slice(self._h_dropout_mask, 1), self.U_f)
        hc_t = F.linear(h_tm1 * get_mask_slice(self._h_dropout_mask, 2), self.U_c)
        ho_t = F.linear(h_tm1 * get_mask_slice(self._h_dropout_mask, 3), self.U_o)

        xi_t = F.linear(input * get_mask_slice(self._input_dropout_mask, 0), self.W_i, self.b_i)
        xf_t = F.linear(input * get_mask_slice(self._input_dropout_mask, 1), self.W_f, self.b_f)
        xc_t = F.linear(input * get_mask_slice(self._input_dropout_mask, 2), self.W_c, self.b_c)
        xo_t = F.linear(input * get_mask_slice(self._input_dropout_mask, 3), self.W_o, self.b_o)

        i_t = F.sigmoid(xi_t + hi_t)
        f_t = F.sigmoid(xf_t + hf_t)
        c_t = f_t * c_tm1 + i_t * F.tanh(xc_t + hc_t)
        o_t = F.sigmoid(xo_t + ho_t)
        h_t = o_t * F.tanh(c_t)

        return h_t, c_t


class LSTM(nn.Module):

    def __init__(self, input_size, hidden_size, bidirectional=False, dropout=0., cell_factory=LSTMCell):
        super(LSTM, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.bidirectional = bidirectional
        self.dropout = dropout
        self.cell_factory = cell_factory
        num_directions = 2 if bidirectional else 1
        self.lstm_cells = []

        for direction in range(num_directions):
            cell = cell_factory(input_size, hidden_size, dropout=dropout)
            self.lstm_cells.append(cell)

            suffix = '_reverse' if direction == 1 else ''
            cell_name = 'cell{}'.format(suffix)
            self.add_module(cell_name, cell)

    def forward(self, input, hidden_state=None):
        is_packed = isinstance(input, PackedSequence)
        if is_packed:
            input, batch_sizes = input
            max_batch_size = batch_sizes[0]
        else: raise NotImplementedError()

        for cell in self.lstm_cells:
            cell.set_dropout_masks(max_batch_size)

        if hidden_state is None:
            num_directions = 2 if self.bidirectional else 1
            hx = torch.autograd.Variable(input.data.new(num_directions,
                                                        max_batch_size,
                                                        self.hidden_size).zero_())

            hidden_state = (hx, hx)

        rec_factory = variable_recurrent_factory(batch_sizes)
        if self.bidirectional:
            layer = (rec_factory(lambda x, h: self.cell(x, h)),
                     rec_factory(lambda x, h: self.cell_reverse(x, h), reverse=True))
        else:
            layer = (rec_factory(lambda x, h: self.cell(x, h)),)

        func = StackedRNN(layer,
                          num_layers=1,
                          lstm=True,
                          dropout=0.,
                          train=self.training)
        next_hidden, output = func(input, hidden_state, weight=[[], []])

        if is_packed:
            output = PackedSequence(output, batch_sizes)
        return output, next_hidden
