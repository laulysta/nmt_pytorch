''' Define the NMTmodelRNN model '''
import torch
import torch.nn as nn
import numpy as np
import NMTmodelRNN.Constants as Constants
import torch.nn.init as init
from torch.autograd import Variable
import torch.nn.functional as F

class LSTM(nn.Module):

    def __init__(self, input_size, hidden_size, batch_first=True):
        """Initialize params."""
        super(PersonaLSTMAttentionDot, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.num_layers = 1
        self.batch_first = batch_first

        self.input_weights = nn.Linear(input_size, 4 * hidden_size)
        self.hidden_weights = nn.Linear(hidden_size, 4 * hidden_size)

    def forward(self, input, hidden, ctx, ctx_mask=None):
        """Propogate input through the network."""
        # tag = None  #
        def recurrence(input, hidden):
            """Recurrence helper."""
            hx, cx = hidden  # n_b x hidden_dim
            gates = self.input_weights(input) + \
                self.hidden_weights(hx)
            ingate, forgetgate, cellgate, outgate = gates.chunk(4, 1)

            ingate = F.sigmoid(ingate)
            forgetgate = F.sigmoid(forgetgate)
            cellgate = F.tanh(cellgate)  # o_t
            outgate = F.sigmoid(outgate)

            cy = (forgetgate * cx) + (ingate * cellgate)
            hy = outgate * F.tanh(cy)  # n_b x hidden_dim

            return hy, cy

        if self.batch_first:
            input = input.transpose(0, 1)

        output = []
        steps = range(input.size(0))
        for i in steps:
            hidden = recurrence(input[i], hidden)
            if isinstance(hidden, tuple):
                output.append(hidden[0])
            else:
                output.append(hidden)

            # output.append(hidden[0] if isinstance(hidden, tuple) else hidden)
            # output.append(isinstance(hidden, tuple) and hidden[0] or hidden)

        output = torch.cat(output, 0).view(input.size(0), *output[0].size())

        if self.batch_first:
            output = output.transpose(0, 1)

        return output, hidden


class LayerNormalization(nn.Module):
    ''' Layer normalization module '''

    def __init__(self, d_hid, eps=1e-3):
        super(LayerNormalization, self).__init__()

        self.eps = eps
        self.a_2 = nn.Parameter(torch.ones(d_hid), requires_grad=True)
        self.b_2 = nn.Parameter(torch.zeros(d_hid), requires_grad=True)

    def forward(self, z):
        if z.size(-1) == 1:
            return z

        mu = torch.mean(z, keepdim=True, dim=-1)
        sigma = torch.std(z, keepdim=True, dim=-1)
        ln_out = (z - mu.expand_as(z)) / (sigma.expand_as(z) + self.eps)
        ln_out = ln_out * self.a_2.expand_as(ln_out) + self.b_2.expand_as(ln_out)

        return ln_out
