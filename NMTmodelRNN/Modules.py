''' Define the NMTmodelRNN model '''
import torch
import torch.nn as nn
import numpy as np
import NMTmodelRNN.Constants as Constants
import torch.nn.init as init
from torch.autograd import Variable
import torch.nn.functional as F



def ortho_weight(ndim):
    W = np.random.randn(ndim, ndim)
    u, s, v = np.linalg.svd(W)
    return u.astype('float32')

def norm_weight(nin, nout=None, scale=None, ortho=True):
    rng = np.random.RandomState(1234)
    if nout is None:
        nout = nin
    if nout == nin and ortho:
        W = ortho_weight(nin)
    else:
        if scale is None:
            scale = np.sqrt(6. / (nin + nout)) 
        W = scale * (2. * rng.rand(nin, nout) - 1.)
    return W.astype('float32')

class GRU(nn.Module):

    def __init__(self, input_size, hidden_size, batch_first=True, layer_norm=True):
        """Initialize params."""
        super(GRU, self).__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.num_layers = 1
        self.batch_first = batch_first

        self.input_weights_x = nn.Linear(input_size, hidden_size)
        self.input_weights_x.weight.data = torch.from_numpy(norm_weight(hidden_size, input_size))
        self.input_weights_x.bias.data.zero_()

        self.input_weights_gates = nn.Linear(input_size, 2 * hidden_size)
        self.input_weights_gates.weight.data[:hidden_size] = torch.from_numpy(norm_weight(hidden_size, input_size))
        self.input_weights_gates.weight.data[hidden_size:hidden_size*2] = torch.from_numpy(norm_weight(hidden_size, input_size))
        self.input_weights_gates.bias.data.zero_()

        self.hidden_weights_x = nn.Linear(hidden_size, hidden_size)
        self.hidden_weights_x.weight.data = torch.from_numpy(ortho_weight(hidden_size))
        self.hidden_weights_x.bias.data.zero_()

        self.hidden_weights_gates = nn.Linear(hidden_size, 2 * hidden_size)
        self.hidden_weights_gates.weight.data[:hidden_size] = torch.from_numpy(ortho_weight(hidden_size))
        self.hidden_weights_gates.weight.data[hidden_size:hidden_size*2] = torch.from_numpy(ortho_weight(hidden_size))
        self.hidden_weights_gates.bias.data.zero_()

        if layer_norm:
            self.ln_x = LayerNormalization(hidden_size)
            self.ln_g = LayerNormalization(hidden_size*2)


    def forward(self, inp, hidden, inp_lens=None):
        """Propogate input through the network."""
        # tag = None  #
        def recurrence(inp, hidden):
            """Recurrence helper."""
            #hx, cx = hidden  # n_b x hidden_dim
            #import ipdb; ipdb.set_trace()
            gates = self.input_weights_gates(inp) + self.hidden_weights_gates(hidden)
            gates = self.ln_g(gates)
            gates = F.sigmoid(gates)
            r, u = gates.chunk(2, 1)

            preactx = self.hidden_weights_x(hidden)
            preactx *= r
            preactx += self.input_weights_x(inp)
            preactx = self.ln_x(preactx)
            h = F.tanh(preactx)

            h = u*hidden + (1.0-u)*h

            return h

        hidden = hidden[0]

        if self.batch_first:
            inp = inp.transpose(0, 1)

        output = []
        steps = range(inp.size(0))
        for i in steps:
            hidden = recurrence(inp[i], hidden)
            output.append(hidden)

        output = torch.cat(output, 0).view(inp.size(0), *output[0].size())

        if self.batch_first:
            output = output.transpose(0, 1)

        #import ipdb; ipdb.set_trace()
        if inp_lens is not None:
            n_timesteps = output.size()[1]
            for ii, ll in enumerate(inp_lens):
                if ll < n_timesteps:
                    output[ii,ll:,:] = 0.

        return output, hidden[None,:,:]


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
