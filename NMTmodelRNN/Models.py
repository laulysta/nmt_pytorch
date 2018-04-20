''' Define the NMTmodelRNN model '''
import torch
import torch.nn as nn
import numpy as np
import NMTmodelRNN.Constants as Constants
import torch.nn.init as init
from torch.autograd import Variable
import torch.nn.functional as F

from torch.nn.utils.rnn import pack_padded_sequence as pack
from torch.nn.utils.rnn import pad_packed_sequence as unpack

__author__ = "Yu-Hsiang Huang"

def position_encoding_init(n_position, d_pos_vec):
    ''' Init the sinusoid position encoding table '''

    # keep dim 0 for padding token position encoding zero vector
    position_enc = np.array([
        [pos / np.power(10000, 2*i/d_pos_vec) for i in range(d_pos_vec)]
        if pos != 0 else np.zeros(d_pos_vec) for pos in range(n_position)])

    position_enc[1:, 0::2] = np.sin(position_enc[1:, 0::2]) # dim 2i
    position_enc[1:, 1::2] = np.cos(position_enc[1:, 1::2]) # dim 2i+1
    return torch.from_numpy(position_enc).type(torch.FloatTensor)

def get_attn_padding_mask(seq_q, seq_k):
    ''' Indicate the padding-related part to mask '''
    assert seq_q.dim() == 2 and seq_k.dim() == 2
    mb_size, len_q = seq_q.size()
    mb_size, len_k = seq_k.size()
    pad_attn_mask = seq_k.data.eq(Constants.PAD).unsqueeze(1)   # bx1xsk
    pad_attn_mask = pad_attn_mask.expand(mb_size, len_q, len_k) # bxsqxsk
    return pad_attn_mask

def get_attn_subsequent_mask(seq):
    ''' Get an attention mask to avoid using the subsequent info.'''
    assert seq.dim() == 2
    attn_shape = (seq.size(0), seq.size(1), seq.size(1))
    subsequent_mask = np.triu(np.ones(attn_shape), k=1).astype('uint8')
    subsequent_mask = torch.from_numpy(subsequent_mask)
    if seq.is_cuda:
        subsequent_mask = subsequent_mask.cuda()
    return subsequent_mask

def flip(x, dim):
    xsize = x.size()
    dim = x.dim() + dim if dim < 0 else dim
    x = x.view(-1, *xsize[dim:])
    x = x.view(x.size(0), x.size(1), -1)[:, getattr(torch.arange(x.size(1)-1, 
                      -1, -1), ('cpu','cuda')[x.is_cuda])().long(), :]
    return x.view(xsize)

def ortho_weight(ndim):
    W = np.random.randn(ndim, ndim)
    u, s, v = np.linalg.svd(W)
    return u.astype('float32')

def norm_weight(nin, nout=None, scale=None, ortho=True):
    #rng = np.random.RandomState(1234)
    if nout is None:
        nout = nin
    if nout == nin and ortho:
        W = ortho_weight(nin)
    else:
        if scale is None:
            scale = np.sqrt(6. / (nin + nout)) 
        W = scale * (2. * np.random.rand(nin, nout) - 1.)
    return W.astype('float32')

def rnn_init_weights(rnn, d_out, d_in):
    for ii in range(len(rnn.all_weights)):
        # init input matrix U
        rnn.all_weights[ii][0].data[:d_out] = torch.from_numpy(norm_weight(d_out, d_in))
        rnn.all_weights[ii][0].data[d_out:d_out*2] = torch.from_numpy(norm_weight(d_out, d_in))
        rnn.all_weights[ii][0].data[d_out*2:d_out*3] = torch.from_numpy(norm_weight(d_out, d_in))

        # init time step matrix U with orthogonal matrix
        rnn.all_weights[ii][1].data[:d_out] = torch.from_numpy(ortho_weight(d_out))
        rnn.all_weights[ii][1].data[d_out:d_out*2] = torch.from_numpy(ortho_weight(d_out))
        rnn.all_weights[ii][1].data[d_out*2:d_out*3] = torch.from_numpy(ortho_weight(d_out))

        # init bias
        rnn.all_weights[ii][2].data.zero_()
        rnn.all_weights[ii][3].data.zero_()

def emb_init_weights(emb, d_voc, d_word_vec):
    emb.weight.data[1:] = torch.from_numpy(norm_weight(d_voc-1, d_word_vec))

def layer_init_weights(layer, d_out, d_in, scale=None, bias=True):
    layer.weight.data = torch.from_numpy(norm_weight(d_in, d_out, scale))
    if bias:
        layer.bias.data.zero_()

class EncoderFast(nn.Module):
    def __init__(self, n_src_vocab, n_max_seq, n_layers=1,
                d_word_vec=512, d_model=512, dropout=0.5,
                nb_lang_src=0, nb_lang_tgt=0, share_enc_dec=False, cuda=False):
        super(EncoderFast, self).__init__()
        self.tt = torch.cuda if cuda else torch
        d_ctx = d_model*2

        self.emb = nn.Embedding(n_src_vocab, d_word_vec, padding_idx=Constants.PAD)
        emb_init_weights(self.emb, n_src_vocab, d_word_vec)

        # if share_enc_dec:
        #     self.rnn = None
        # else:
        self.rnn = nn.GRU(
                    input_size=d_word_vec+nb_lang_src+nb_lang_tgt,
                    hidden_size=d_model,
                    num_layers=n_layers,
                    dropout=dropout,
                    batch_first=True,
                    bidirectional=True)
        rnn_init_weights(self.rnn, d_model, d_word_vec+nb_lang_src+nb_lang_tgt)

        self.drop = nn.Dropout(p=dropout)
        self.n_layers = n_layers
        self.d_model = d_model
        self.tt = torch.cuda if cuda else torch

    def forward(self, x_in, x_in_lens, l_in=None, src_lang_oneHot=None, tgt_lang_oneHot=None):
        # x_in : (batch_size, x_seq_len)
        # x_in_lens : (batch_size)
        batch_size, x_seq_len = x_in.size()

        h_0 = Variable( self.tt.FloatTensor(self.n_layers * 2, \
                                            batch_size, self.d_model).zero_() )
        x_in_emb = self.emb(x_in) # (batch_size, x_seq_len, D_emb)
        x_in_emb = self.drop(x_in_emb)

        if src_lang_oneHot is not None:
            tmp = src_lang_oneHot[:,None,:].repeat(1,x_seq_len,1)
            x_in_emb = torch.cat((tmp, x_in_emb), dim=2)

        if tgt_lang_oneHot is not None:
            tmp = tgt_lang_oneHot[:,None,:].repeat(1,x_seq_len,1)
            x_in_emb = torch.cat((tmp, x_in_emb), dim=2)

        # Lengths data is wrapped inside a Variable.
        x_in_lens = x_in_lens.data.view(-1).tolist()
        pack = torch.nn.utils.rnn.pack_padded_sequence(x_in_emb, x_in_lens, batch_first=True)

        # input (batch_size, x_seq_len, D_emb)
        # h_0 (num_layers * num_dir, batch_size, D_hid)
        top_layer, h_n = self.rnn(pack, h_0)
        # top_layer (batch_size, x_seq_len, D_hid * num_dir)
        # h_n (num_layers * num_dir, batch_size, D_hid)

        # attentional decoder : return the entire sequence of h_n
        out, outlen = torch.nn.utils.rnn.pad_packed_sequence(top_layer, batch_first = True)
        return out.contiguous() # (batch_size, x_seq_len, D_hid * num_dir)

class Encoder(nn.Module):
    def __init__(self, n_src_vocab, n_max_seq, n_layers=1,
                d_word_vec=512, d_model=512, dropout=0.5,
                nb_lang_src=0, nb_lang_tgt=0, cuda=False):
        super(Encoder, self).__init__()
        self.tt = torch.cuda if cuda else torch
        d_ctx = d_model*2

        self.emb = nn.Embedding(n_src_vocab, d_word_vec, padding_idx=Constants.PAD)
        emb_init_weights(self.emb, n_src_vocab, d_word_vec)
        
        self.rnn1 = nn.GRU(
                    input_size=d_word_vec+nb_lang_src+nb_lang_tgt,
                    hidden_size=d_model,
                    num_layers=n_layers,
                    dropout=dropout,
                    batch_first=True,
                    bidirectional=False)
        rnn_init_weights(self.rnn1, d_model, d_word_vec+nb_lang_src+nb_lang_tgt)

        self.rnn2 = nn.GRU(
                    input_size=d_word_vec+nb_lang_src+nb_lang_tgt,
                    hidden_size=d_model,
                    num_layers=n_layers,
                    dropout=dropout,
                    batch_first=True,
                    bidirectional=False)
        rnn_init_weights(self.rnn2, d_model, d_word_vec+nb_lang_src+nb_lang_tgt)

        self.drop = nn.Dropout(p=dropout)
        self.n_layers = n_layers
        self.d_model = d_model
        self.tt = torch.cuda if cuda else torch
        self.d_ctx = d_ctx

    def forward(self, x_in, x_in_lens, l_in=None, src_lang_oneHot=None, tgt_lang_oneHot=None):
        # x_in : (batch_size, x_seq_len)
        # x_in_lens : (batch_size)

        batch_size, x_seq_len = x_in.size()

        h_0 = Variable( self.tt.FloatTensor(self.n_layers, \
                                            batch_size, self.d_model).zero_() )
        
        x_in_emb = self.emb(x_in) # (batch_size, x_seq_len, D_emb)
        x_in_emb = self.drop(x_in_emb)

        if src_lang_oneHot is not None:
            tmp = src_lang_oneHot[:,None,:].repeat(1,x_seq_len,1)
            x_in_emb = torch.cat((tmp, x_in_emb), dim=2)

        if tgt_lang_oneHot is not None:
            tmp = tgt_lang_oneHot[:,None,:].repeat(1,x_seq_len,1)
            x_in_emb = torch.cat((tmp, x_in_emb), dim=2)

        # Lengths data is wrapped inside a Variable.
        x_in_lens = x_in_lens.data.view(-1).tolist()

        #Create reverse seq emb
        x_in_emb_r = torch.zeros_like(x_in_emb)
        for ii, seq_len in enumerate(x_in_lens):
            tmp = x_in_emb[ii,:seq_len,:]
            x_in_emb_r[ii,:seq_len,:] = flip(tmp, 0)
        # Add tgt_lang rep
        if l_in is not None:
            l_in_emb = self.emb(l_in)

            x_in_emb = torch.cat((l_in_emb, x_in_emb), dim=1) # (batch_size, x_seq_len+1, D_emb)
            x_in_emb_r = torch.cat((l_in_emb, x_in_emb_r), dim=1) # (batch_size, x_seq_len+1, D_emb)
            x_in_lens = [x+1 for x in x_in_lens]

        pack = torch.nn.utils.rnn.pack_padded_sequence(x_in_emb, x_in_lens, batch_first=True)
        pack_r = torch.nn.utils.rnn.pack_padded_sequence(x_in_emb_r, x_in_lens, batch_first=True)
        
        # input (batch_size, x_seq_len, D_emb)
        # h_0 (num_layers * num_dir, batch_size, D_hid)
        top_layer, h_n = self.rnn1(pack, h_0)
        top_layer_r, h_n_r = self.rnn2(pack_r, h_0)
        # top_layer (batch_size, x_seq_len, D_hid * num_dir)
        # h_n (num_layers * num_dir, batch_size, D_hid)

        # attentional decoder : return the entire sequence of h_n
        out1, outlen1 = torch.nn.utils.rnn.pad_packed_sequence(top_layer, batch_first = True)
        out_r, outlen_r = torch.nn.utils.rnn.pad_packed_sequence(top_layer_r, batch_first = True)

        # Remove tgt_lang rep
        if l_in is not None:
            out1 = out1[:,1:]
            out_r = out_r[:,1:]
            x_in_lens = [x-1 for x in x_in_lens]

        # Reverse back
        out_r = out_r.contiguous()
        out2 = torch.zeros_like(out_r)
        for ii, seq_len in enumerate(x_in_lens):
            tmp = out_r[ii,:seq_len,:]
            out2[ii,:seq_len,:] = flip(tmp, 0)

        out = torch.cat((out1, out2), dim=2)

        return out.contiguous() # (batch_size, x_seq_len, D_hid * num_dir)


def xlen_to_mask_rnn(x_len, tt):
    batch_size = len(x_len)
    maxlen = max(x_len)
    ans = tt.ByteTensor(batch_size, maxlen).fill_(1)
    for idx in range(batch_size):
        ans[idx][:x_len[idx]] = 0
    return ans
    # (batch_size, x_seq_len)

class Decoder(nn.Module):
    # Base recurrent attention-based decoder class.
    def __init__(
             self, n_tgt_vocab, n_max_seq, n_layers=2,
             d_word_vec=512, d_model=512, dropout=0.5, no_proj_share_weight=True,
             nb_lang_src=0, nb_lang_tgt=0, cuda=False):
        super(Decoder, self).__init__()
        self.tt = torch.cuda if cuda else torch
        d_ctx = d_model*2

        self.emb = nn.Embedding(n_tgt_vocab, d_word_vec, padding_idx=0)
        emb_init_weights(self.emb, n_tgt_vocab, d_word_vec)

        
        #self.rnn = nn.GRUCell(d_ctx+d_word_vec, d_model)
        self.rnn1 = nn.GRU(d_word_vec, d_model, \
                           n_layers, dropout=dropout, batch_first=True)
        rnn_init_weights(self.rnn1, d_model, d_word_vec)

        self.rnn2 = nn.GRU(nb_lang_src+nb_lang_tgt+d_ctx, d_model, \
                           n_layers, dropout=dropout, batch_first=True)
        rnn_init_weights(self.rnn2, d_model, nb_lang_src+nb_lang_tgt+d_ctx)

        self.drop = nn.Dropout(p=dropout)
        self.ctx_to_s0 = nn.Linear(nb_lang_src+nb_lang_tgt+d_ctx, n_layers * d_model)
        layer_init_weights(self.ctx_to_s0, nb_lang_src+nb_lang_tgt+d_ctx, n_layers * d_model)

        self.y_to_ctx = nn.Linear(d_word_vec, d_ctx)
        layer_init_weights(self.y_to_ctx, d_word_vec, d_ctx)
        self.s_to_ctx = nn.Linear(d_model * n_layers, d_ctx, bias=False)
        layer_init_weights(self.s_to_ctx, d_model * n_layers, d_ctx, bias=False)
        self.h_to_ctx = nn.Linear(d_ctx, d_ctx, bias=False)
        layer_init_weights(self.h_to_ctx, d_ctx, d_ctx, bias=False)
        self.ctx_to_score = nn.Linear(d_ctx, 1)
        layer_init_weights(self.ctx_to_score, d_ctx, 1)

        self.y_to_fin = nn.Linear(d_word_vec, d_word_vec)
        layer_init_weights(self.y_to_fin, d_word_vec, d_word_vec)
        self.c_to_fin = nn.Linear(d_ctx, d_word_vec, bias=False)
        layer_init_weights(self.c_to_fin, d_ctx, d_word_vec, bias=False)
        self.s_to_fin = nn.Linear(d_model, d_word_vec, bias=False)
        layer_init_weights(self.s_to_fin, d_model, d_word_vec, bias=False)

        if no_proj_share_weight:
            self.fin_to_voc = nn.Linear(d_word_vec, n_tgt_vocab)
            layer_init_weights(self.fin_to_voc, d_word_vec, n_tgt_vocab, scale=0.5)
        else:
            self.fin_to_voc = nn.Linear(d_word_vec, n_tgt_vocab, bias=False)
            layer_init_weights(self.fin_to_voc, d_word_vec, n_tgt_vocab, scale=0.5, bias=False)

        if not no_proj_share_weight:
            # Share the weight matrix between tgt word embedding/projection
            assert self.emb.weight.size() == self.fin_to_voc.weight.size()
            self.emb.weight = self.fin_to_voc.weight

        self.n_layers = n_layers
        self.d_ctx = d_ctx
        self.d_model = d_model
        self.n_tgt_vocab = n_tgt_vocab
        self.d_word_vec = d_word_vec
        self.n_max_seq = n_max_seq

    def forward(self, h_in, h_in_len, y_in, l_in=None, src_lang_oneHot=None, tgt_lang_oneHot=None):
        # h_in : (batch_size, x_seq_len, d_ctx)
        # h_in_len : (batch_size)
        # y_in : (batch_size, y_seq_len)
        batch_size, y_seq_len = y_in.size()
        x_seq_len = h_in.size()[1]
        h_in_len = h_in_len.data.view(-1)
        xmask = xlen_to_mask_rnn(h_in_len.tolist(), self.tt) # (batch_size, x_seq_len)

        s_0_ = torch.sum(h_in, 1) # (batch_size, D_hid_enc * num_dir_enc)
        s_0_ = torch.div( s_0_, Variable(self.tt.FloatTensor(h_in_len.tolist()).view(-1,1)) )
        s_0_andMore = s_0_
        if src_lang_oneHot is not None:
            s_0_andMore = torch.cat((src_lang_oneHot, s_0_andMore), dim=1)
        if tgt_lang_oneHot is not None:
            s_0_andMore = torch.cat((tgt_lang_oneHot, s_0_andMore), dim=1)
        s_0 = self.ctx_to_s0(s_0_andMore)
        s_0 = F.tanh(s_0)
        s_t = s_0 # (batch_size, n_layers * d_model)
        s_t = s_t.view(batch_size, self.n_layers, self.d_model).transpose(0,1).contiguous() \
                # (n_layers, batch_size, d_model)

        y_in_emb = self.emb(y_in) # (batch_size, y_seq_len, d_word_vec)
        y_in_emb = self.drop(y_in_emb) # (batch_size, y_seq_len, d_word_vec)

        if l_in is not None:
            l_in_emb = self.emb(l_in)
            y_in_emb = torch.cat((l_in_emb, y_in_emb), dim=1) # (batch_size, x_seq_len+1, D_emb)
            y_seq_len += 1

        # y_in_emb_andMore = y_in_emb

        # if src_lang_oneHot is not None:
        #     tmp = src_lang_oneHot[:,None,:].repeat(1,y_seq_len,1)
        #     y_in_emb_andMore = torch.cat((tmp, y_in_emb_andMore), dim=2)

        # if tgt_lang_oneHot is not None:
        #     tmp = tgt_lang_oneHot[:,None,:].repeat(1,y_seq_len,1)
        #     y_in_emb_andMore = torch.cat((tmp, y_in_emb_andMore), dim=2)

        h_in_big = h_in.view(-1, self.d_ctx) \
                # (batch_size * x_seq_len, d_ctx) 

        ctx_h = self.h_to_ctx( h_in_big ).view(batch_size, x_seq_len, self.d_ctx)
                # (batch_size, x_seq_len, d_ctx)

        logits = []
        for idx in range(y_seq_len):
            #ctx_s_t_ = s_tm1.transpose(0,1).contiguous().view(batch_size, -1) \
            #        # (batch_size, d_model * n_layers)

            # in (batch_size, 1, D_emb_dec)
            # s_t (num_layers_dec, batch_size, D_hid_dec)
            _, s_t_ = self.rnn1( y_in_emb[:,idx,:][:,None,:], s_t )
            # out (batch_size, 1, D_hid_dec)
            # s_t (num_layers_dec, batch_size, D_hid_dec)
            ctx_s_t_ = s_t_.transpose(0,1).contiguous().view(batch_size, -1) \
                    # (batch_size, D_hid_dec * num_layers_dec)


            ctx_y = self.y_to_ctx( y_in_emb[:,idx,:] )[:,None,:] # (batch_size, 1, d_ctx)
            ctx_s = self.s_to_ctx( ctx_s_t_ )[:,None,:]
            ctx = F.tanh(ctx_y + ctx_s + ctx_h) # (batch_size, x_seq_len, d_ctx)
            ctx = ctx.view(-1, self.d_ctx) # (batch_size * x_seq_len, d_ctx)

            score = self.ctx_to_score(ctx).view(batch_size, -1) # (batch_size, x_seq_len)
            score.data.masked_fill_(xmask, -float('inf'))
            score = F.softmax(score, dim=1)
            score = score[:,:,None] # (batch_size, x_seq_len, 1)

            c_t = torch.mul( h_in, score ) # (batch_size, x_seq_len, d_ctx)
            c_t = torch.sum( c_t, 1) # (batch_size, d_ctx)
            c_t_andMore = c_t
            if src_lang_oneHot is not None:
                c_t_andMore = torch.cat((src_lang_oneHot, c_t_andMore), dim=1)

            if tgt_lang_oneHot is not None:
                c_t_andMore = torch.cat((tgt_lang_oneHot, c_t_andMore), dim=1)

            # in (batch_size, 1, d_ctx)
            # s_t (n_layers, batch_size, d_model)
            # out, s_t = self.rnn( torch.cat((y_in_emb_andMore[:,idx,:][:,None,:], c_t[:,None,:]), dim=2), s_tm1 )
            out, s_t = self.rnn2( c_t_andMore[:,None,:], s_t_ )

            fin_y = self.y_to_fin( y_in_emb[:,idx,:] ) # (batch_size, d_word_vec)
            fin_c = self.c_to_fin( c_t ) # (batch_size, d_word_vec)
            fin_s = self.s_to_fin( out.view(-1, self.d_model) ) # (batch_size, d_word_vec)
            fin = F.tanh( fin_y + fin_c + fin_s )
            fin = self.drop(fin)

            logit = self.fin_to_voc( fin ) # (batch_size, vocab_size)
            logits.append( logit )

            # logits : list of (batch_size, vocab_size) vectors
        
        ans = torch.cat(logits, 0) # (y_seq_len * batch_size, vocab_size)
        ans = ans.view(y_seq_len, batch_size, self.n_tgt_vocab).transpose(0,1).contiguous()
        if l_in is not None:
            ans = ans[:,1:]
            ans = ans.contiguous()
            y_seq_len -= 1
        
        return ans.view(batch_size * y_seq_len, -1)

    def greedy_search(self, h_in, h_in_len, l_in=None, src_lang_oneHot=None, tgt_lang_oneHot=None):
        # h_in : (batch_size, x_seq_len, d_ctx)
        # h_in_len : (batch_size)
        h_in_len = h_in_len.data.view(-1).tolist()

        batch_size, x_seq_len = h_in.size()[0], h_in.size()[1]
        xmask = xlen_to_mask_rnn(h_in_len, self.tt) # (batch_size, x_seq_len)

        s_t = torch.sum(h_in, 1) # (batch_size, d_ctx)
        s_t = torch.div( s_t, Variable(self.tt.FloatTensor(h_in_len).view(batch_size, 1)) )
        s_t_andMore = s_t
        if src_lang_oneHot is not None:
            s_t_andMore = torch.cat((src_lang_oneHot, s_t_andMore), dim=1)
        if tgt_lang_oneHot is not None:
            s_t_andMore = torch.cat((tgt_lang_oneHot, s_t_andMore), dim=1)
        s_t = self.ctx_to_s0(s_t_andMore)
        s_t = F.tanh(s_t)
        s_t = s_t.view(batch_size, self.n_layers, self.d_model).transpose(0,1).contiguous() \
                # (n_layers, batch_size, d_model)

        if l_in is not None:
            y_in_emb = self.emb(l_in)# (batch_size, 1, d_word_vec)
        else:
            start = [2 for ii in range(batch_size)] # NOTE <BOS> is always 2
            ft = self.tt.LongTensor(start)[:,None] # (batch_size, 1)
            y_in_emb = self.emb( Variable( ft ) ) # (batch_size, 1, d_word_vec)

        # y_in_emb_andMore = y_in_emb

        # if src_lang_oneHot is not None:
        #     y_in_emb_andMore = torch.cat((src_lang_oneHot[:,None,:], y_in_emb_andMore), dim=2)

        # if tgt_lang_oneHot is not None:
        #     y_in_emb_andMore = torch.cat((tgt_lang_oneHot[:,None,:], y_in_emb_andMore), dim=2)

        h_in_big = h_in.contiguous().view(batch_size * x_seq_len, self.d_ctx ) \
                # (batch_size * x_seq_len, D_hid_enc * num_dir_enc) 
        ctx_h = self.h_to_ctx( h_in_big ).view(batch_size, x_seq_len, self.d_ctx)

        gen_idx = [[] for ii in range(batch_size)]
        done = np.array( [False for ii in range(batch_size)] )

        for idx in range(self.n_max_seq):
            # in (batch_size, 1, d_word_vec)
            # s_t (n_layers, batch_size, d_model)
            #_, s_t_ = self.rnn1( y_in_emb, s_t )
            # ctx_s_t_ = s_tm1.transpose(0,1).contiguous().view(batch_size, self.n_layers * self.d_model) \
            #         # (batch_size, n_layers * d_model)
            _, s_t_ = self.rnn1( y_in_emb, s_t )
            # out (batch_size, 1, D_hid_dec)
            # s_t (num_layers_dec, batch_size, D_hid_dec)
            ctx_s_t_ = s_t_.transpose(0,1).contiguous().view(batch_size, self.n_layers * self.d_model) \
                    # (batch_size, num_layers_dec * D_hid_dec)

            ctx_y = self.y_to_ctx( y_in_emb.view(batch_size, self.d_word_vec) )[:,None,:]
            ctx_s = self.s_to_ctx( ctx_s_t_ )[:,None,:]
            ctx = F.tanh(ctx_y + ctx_s + ctx_h)
            ctx = ctx.view(batch_size * x_seq_len, self.d_ctx)

            score = self.ctx_to_score(ctx).view(batch_size, -1) # (batch_size, x_seq_len)
            score.data.masked_fill_(xmask, -float('inf'))
            score = F.softmax(score, dim=1)
            score = score[:,:,None] # (batch_size, x_seq_len, 1)

            c_t = torch.mul( h_in, score ) # (batch_size, x_seq_len, d_ctx)
            c_t = torch.sum( c_t, 1) # (batch_size, d_ctx)

            c_t_andMore = c_t
            if src_lang_oneHot is not None:
                c_t_andMore = torch.cat((src_lang_oneHot, c_t_andMore), dim=1)

            if tgt_lang_oneHot is not None:
                c_t_andMore = torch.cat((tgt_lang_oneHot, c_t_andMore), dim=1)

            # in (batch_size, 1, d_ctx)
            # s_t (n_layers, batch_size, d_model)
            #out, s_t = self.rnn( torch.cat((y_in_emb_andMore[:,0,:][:,None,:], c_t[:,None,:]), dim=2), s_tm1 )
            out, s_t = self.rnn2( c_t_andMore[:,None,:], s_t_ )
            # out (batch_size, 1, D_hid_dec)
            # s_t (num_layers_dec, batch_size, D_hid_dec)

            if idx == 0 and l_in is not None:
                start = [2 for ii in range(batch_size)] # NOTE <BOS> is always 2
                ft = self.tt.LongTensor(start)[:,None] # (batch_size, 1)

                y_in_emb = self.emb( Variable( ft ) ) # (batch_size, 1, d_word_vec)
                s_tm1 = s_t
                continue

            fin_y = self.y_to_fin( y_in_emb.view(batch_size, self.d_word_vec) ) # (batch_size, d_word_vec)
            fin_c = self.c_to_fin( c_t ) # (batch_size, d_word_vec)
            fin_s = self.s_to_fin( out.view(batch_size, self.d_model) ) # (batch_size, d_word_vec)
            fin = F.tanh( fin_y + fin_c + fin_s )

            logit = self.fin_to_voc( fin ) # (batch_size, vocab_size)
            topv, topi = logit.data.topk(1)
            top1 = topi.view(batch_size).cpu().numpy()

            for ii in range(batch_size):
                if top1[ii] == 3: # NOTE <EOS> is always 3
                    done[ii] = True
                if not done[ii]:
                    gen_idx[ii].append( top1[ii].item() ) # use .item() to convert numpy.int64 to int

            if np.array_equal( done, np.array( [True for x in range(batch_size) ] ) ):
                break

            y_in_emb = self.emb( Variable( topi ) )

            # y_in_emb_andMore = y_in_emb

            # if src_lang_oneHot is not None:
            #     y_in_emb_andMore = torch.cat((src_lang_oneHot[:,None,:], y_in_emb_andMore), dim=2)

            # if tgt_lang_oneHot is not None:
            #     y_in_emb_andMore = torch.cat((tgt_lang_oneHot[:,None,:], y_in_emb_andMore), dim=2)

        return gen_idx

    def beam_search(self, h_in, h_in_len, width, l_in=None, src_lang_oneHot=None, tgt_lang_oneHot=None): # (batch_size, x_seq_len, D_hid_enc * num_dir_enc)
        voc_size, batch_size, x_seq_len = self.vocab_size[ self.trg ], h_in.size()[0], h_in.size()[1]
        live = [ [ ( 0.0, [ 2 ], 2 ) ] for ii in range(batch_size) ]
        dead = [ [] for ii in range(batch_size) ]
        num_dead = [0 for ii in range(batch_size)]
        xmask = xlen_to_mask_rnn(h_in_len, self.tt)[:,None,:] # (batch_size, 1, x_seq_len)

        s_t = torch.sum(h_in, 1) # (batch_size, D_hid_enc * num_dir_enc)
        s_t = torch.div( s_t, Variable(self.tt.FloatTensor(h_in_len).view(batch_size, 1)) )
        s_t_andMore = s_t
        if src_lang_oneHot is not None:
            s_t_andMore = torch.cat((src_lang_oneHot, s_t_andMore), dim=1)
        if tgt_lang_oneHot is not None:
            s_t_andMore = torch.cat((tgt_lang_oneHot, s_t_andMore), dim=1)
        s_t = self.ctx_to_s0(s_t) # (batch_size, num_layers_dec * D_hid_dec)
        s_t = F.tanh(s_t)
        s_t = s_t.view(batch_size, self.num_layers_dec, self.D_hid_dec).transpose(0,1).contiguous() \
                # (num_layers_dec, batch_size, D_hid_dec)

        if l_in is not None:
            input = self.emb(l_in)# (batch_size, 1, d_word_vec)
        else:
            ft = self.tt.LongTensor( [ 2 for ii in range(batch_size) ] )[:,None]
            input = self.emb( Variable( ft ) ) # (batch_size, 1, D_emb)

        h_in_big = h_in.contiguous().view(-1, self.D_hid_enc * self.num_dir_enc ) \
                # (batch_size * x_seq_len, D_hid_enc * num_dir_enc)
        ctx_h = self.h_to_ctx( h_in_big ).view(batch_size, 1, x_seq_len, self.D_ctx) \
                # NOTE (batch_size, 1, x_seq_len, D_ctx)

        #max_len_gen = max(h_in_len) + self.dec_trg_offset
        #for tidx in range(max_len_gen):
        for idx in range(self.n_max_seq):
            cwidth = 1 if tidx == 0 else width
            # input (batch_size * width, 1, D_emb_dec)
            # s_t (num_layers_dec, batch_size * width, D_hid_dec)
            _, s_t_ = self.rnn1( input, s_t )
            # out (batch_size * width, 1, D_hid_dec)
            # s_t (num_layers_dec, batch_size * width, D_hid_dec)
            ctx_s_t_ = s_t_.transpose(0,1).contiguous().view(batch_size * cwidth, -1) \
                    # (batch_size * width, num_layers_dec * D_hid_dec)

            ctx_y = self.y_to_ctx( input.view(-1, self.D_emb_dec) ).view(batch_size, cwidth, 1, self.D_ctx)
            ctx_s = self.s_to_ctx( ctx_s_t_ ).view(batch_size, cwidth, 1, self.D_ctx)
            ctx = F.tanh(ctx_y + ctx_s + ctx_h) # (batch_size, cwidth, x_seq_len, D_ctx)
            ctx = ctx.view(batch_size * cwidth * x_seq_len, self.D_ctx)

            score = self.ctx_to_score(ctx).view(batch_size, -1, x_seq_len) # (batch_size, cwidth, x_seq_len)
            score.data.masked_fill_(xmask.repeat(1, cwidth, 1), -float('inf'))
            score = F.softmax( score.view(-1, x_seq_len) ).view(batch_size, -1, x_seq_len)
            score = score.view(batch_size, cwidth, x_seq_len, 1) # (batch_size, width, x_seq_len, 1)

            c_t = torch.mul( h_in.view(batch_size, 1, x_seq_len, -1), score ) # (batch_size, width, x_seq_len, D_hid_enc * num_dir_enc)
            c_t = torch.sum( c_t, 2).view(batch_size * cwidth, -1) # (batch_size * width, D_hid_enc * num_dir_enc)
            
            c_t_andMore = c_t
            if src_lang_oneHot is not None:
                c_t_andMore = torch.cat((src_lang_oneHot, c_t_andMore), dim=1)

            if tgt_lang_oneHot is not None:
                c_t_andMore = torch.cat((tgt_lang_oneHot, c_t_andMore), dim=1)


            # c_t (batch_size * width, 1, D_hid_enc * num_dir_enc)
            # s_t (num_layers_dec, batch_size * width, D_hid_dec)
            out, s_t = self.rnn2( c_t_andMore[:,None,:], s_t_ )
            # out (batch_size * width, 1, D_hid_dec)
            # s_t (num_layers_dec, batch_size * width, D_hid_dec)

            fin_y = self.y_to_fin( input.view(-1, self.D_emb_dec) ) # (batch_size * width, D_fin)
            fin_c = self.c_to_fin( c_t ) # (batch_size * width, D_fin)
            fin_s = self.s_to_fin( out.view(-1, self.D_hid_dec) ) # (batch_size * width, D_fin)
            fin = F.tanh( fin_y + fin_c + fin_s )

            cur_prob = F.log_softmax( self.fin_to_voc( fin.view(-1, self.D_fin) ))\
                    .view(batch_size, cwidth, voc_size).data # (batch_size, width, vocab_size)
            pre_prob = self.tt.FloatTensor( [ [ x[0] for x in ee ] for ee in live ] ).view(batch_size, cwidth, 1) \
                    # (batch_size, width, 1)
            total_prob = cur_prob + pre_prob # (batch_size, cwidth, voc_size)
            total_prob = total_prob.view(batch_size, -1)

            _, topi_s = total_prob.topk(width, dim=1)
            topv_s = cur_prob.view(batch_size, -1).gather(1, topi_s)
            # (batch_size, width)

            new_live = [ [] for ii in range(batch_size) ]
            for bidx in range(batch_size):
                num_live = width - num_dead[bidx]
                if num_live > 0:
                    tis = topi_s[bidx][:num_live]
                    tvs = topv_s[bidx][:num_live]
                    for eidx, (topi, topv) in enumerate(zip(tis, tvs)): # NOTE max width times
                        if topi % voc_size == 3 :
                            dead[bidx].append( (  live[bidx][ topi // voc_size ][0] + topv, \
                                                  live[bidx][ topi // voc_size ][1] + [ topi % voc_size ],\
                                                  topi) )
                            num_dead[bidx] += 1
                        else:
                            new_live[bidx].append( (    live[bidx][ topi // voc_size ][0] + topv, \
                                                        live[bidx][ topi // voc_size ][1] + [ topi % voc_size ],\
                                                        topi) )
                while len(new_live[bidx]) < width:
                    new_live[bidx].append( (    -99999999999, \
                                                [0],\
                                                0) )
            live = new_live

            if num_dead == [width for ii in range(batch_size)]:
                break

            in_vocab_idx = [ [ x[2] % voc_size for x in ee ] for ee in live ] # NOTE batch_size first
            input = self.emb( Variable( self.tt.LongTensor( in_vocab_idx ) ).view(-1) )\
                    .view(-1, 1, self.D_emb_dec) # input (batch_size * width, 1, D_emb)

            in_width_idx = [ [ x[2] // voc_size + bbidx * cwidth for x in ee ] for bbidx, ee in enumerate(live) ] \
                    # live : (batch_size, width)
            s_t = s_t.index_select( 1, Variable( self.tt.LongTensor( in_width_idx ).view(-1) ) ).\
                    view(self.num_layers_dec, batch_size * width, self.D_hid_dec)
            # h_0 (num_layers, batch_size * width, D_hid)

        for bidx in range(batch_size):
            if num_dead[bidx] < width:
                for didx in range( width - num_dead[bidx] ):
                    (a, b, c) = live[bidx][didx]
                    dead[bidx].append( (a, b, c)  )

        dead_ = [ [ ( a / ( math.pow(5+len(b), self.beam_alpha) / math.pow(5+1, self.beam_alpha) ) , b, c) for (a,b,c) in ee] for ee in dead]
        ans = []
        for dd_ in dead_:
            dd = sorted( dd_, key=operator.itemgetter(0), reverse=True )
            ans.append( dd[0][1] )
        return ans


#class NMTmodel(nn.Module):
class NMTmodelRNN(nn.Module):
    ''' A sequence to sequence model with attention mechanism. '''

    def __init__(
            self, n_src_vocab, n_tgt_vocab, n_max_seq, n_layers=2,
            d_word_vec=512, d_model=512, dropout=0.1,
            no_proj_share_weight=True, embs_share_weight=True,
            enc_lang=False, dec_lang=False,
            enc_srcLang_oh=False, enc_tgtLang_oh=False, dec_srcLang_oh=False, dec_tgtLang_oh=False,
            srcLangIdx2oneHotIdx={}, tgtLangIdx2oneHotIdx={},
            share_bidir=False, share_enc_dec=False, share_dec_temp=False, cuda=False):


        self.n_layers = n_layers

        super(NMTmodelRNN, self).__init__()

        self.decoder = Decoder(
            n_tgt_vocab, n_max_seq, n_layers=n_layers,
            d_word_vec=d_word_vec, d_model=d_model,
            dropout=dropout, no_proj_share_weight = no_proj_share_weight,
            cuda=cuda,
            nb_lang_src=len(srcLangIdx2oneHotIdx)*dec_srcLang_oh,
            nb_lang_tgt=len(tgtLangIdx2oneHotIdx)*dec_tgtLang_oh)

        
        # self.encoder = Encoder(n_src_vocab, n_max_seq, n_layers=n_layers,
        #                         d_word_vec=d_word_vec, d_model=d_model,
        #                         dropout=dropout,
        #                         nb_lang_src=len(srcLangIdx2oneHotIdx)*enc_srcLang_oh,
        #                         nb_lang_tgt=len(tgtLangIdx2oneHotIdx)*enc_tgtLang_oh,
        #                         cuda=cuda)

        #assert not (enc_srcLang_oh or enc_tgtLang_oh), "Not implemented"
        self.encoder = EncoderFast(n_src_vocab, n_max_seq,
                                    d_word_vec=d_word_vec, d_model=d_model,
                                    dropout=dropout,
                                    nb_lang_src=len(srcLangIdx2oneHotIdx)*enc_srcLang_oh,
                                    nb_lang_tgt=len(tgtLangIdx2oneHotIdx)*enc_tgtLang_oh,
                                    share_enc_dec=share_enc_dec,
                                    cuda=cuda)

        if share_enc_dec:
            assert self.encoder.rnn.weight_ih_l0.size() == self.decoder.rnn1.weight_ih_l0.size(), \
            "To share the encoder and decoder RNN, the input matrix size shall be the same."
            self.encoder.rnn.weight_ih_l0 = self.decoder.rnn1.weight_ih_l0
            self.encoder.rnn.weight_hh_l0 = self.decoder.rnn1.weight_hh_l0
            self.encoder.rnn.bias_ih_l0 = self.decoder.rnn1.bias_ih_l0
            self.encoder.rnn.bias_hh_l0 = self.decoder.rnn1.bias_hh_l0

            self.encoder.rnn.weight_ih_l0_reverse = self.decoder.rnn1.weight_ih_l0
            self.encoder.rnn.weight_hh_l0_reverse = self.decoder.rnn1.weight_hh_l0
            self.encoder.rnn.bias_ih_l0_reverse = self.decoder.rnn1.bias_ih_l0
            self.encoder.rnn.bias_hh_l0_reverse = self.decoder.rnn1.bias_hh_l0
        elif share_bidir:
            self.encoder.rnn.weight_ih_l0_reverse = self.encoder.rnn.weight_ih_l0
            self.encoder.rnn.weight_hh_l0_reverse = self.encoder.rnn.weight_hh_l0
            self.encoder.rnn.bias_ih_l0_reverse = self.encoder.rnn.bias_ih_l0
            self.encoder.rnn.bias_hh_l0_reverse = self.encoder.rnn.bias_hh_l0

        if share_dec_temp:
            self.decoder.rnn2.weight_hh_l0 = self.decoder.rnn1.weight_hh_l0
            self.decoder.rnn2.bias_hh_l0 = self.decoder.rnn1.bias_hh_l0

        if embs_share_weight:
            # Share the weight matrix between src/tgt word embeddings
            # assume the src/tgt word vec size are the same
            assert n_src_vocab == n_tgt_vocab, \
            "To share word embedding table, the vocabulary size of src/tgt shall be the same."
            self.encoder.emb.weight = self.decoder.emb.weight

        self.enc_lang = enc_lang
        self.dec_lang = dec_lang
        self.enc_srcLang_oh = enc_srcLang_oh
        self.enc_tgtLang_oh = enc_tgtLang_oh
        self.dec_srcLang_oh = dec_srcLang_oh
        self.dec_tgtLang_oh = dec_tgtLang_oh
        
        self.srcLangIdx2oneHotIdx = srcLangIdx2oneHotIdx
        self.tgtLangIdx2oneHotIdx = tgtLangIdx2oneHotIdx
        self.tt = torch.cuda if cuda else torch

    def lang2oneHot(self, lang_seq, langIdx2oneHotIdx):
        oh = self.tt.FloatTensor(lang_seq.size()[0], len(langIdx2oneHotIdx)).zero_()
        
        lang_seq = lang_seq.data.tolist()
        for ii, ll in enumerate(lang_seq):
            idx = langIdx2oneHotIdx[ll[0]]
            oh[ii][idx] = 1.0


        oh = Variable(oh)

        return oh

    def forward(self, src, tgt, src_lang=None, tgt_lang=None):
        # src_seq and src_pos: sent_len * batch_size
        src_seq, src_pos = src
        tgt_seq_ori, tgt_pos = tgt
        tgt_lang_seq, tgt_lang_pos = tgt_lang if tgt_lang is not None else (None, None)
        src_lang_seq, src_lang_pos = src_lang if src_lang is not None else (None, None)

        lengths_seq_tgt_ori, idx_tgt = tgt_pos.max(1)
        lengths_seq_tgt = lengths_seq_tgt_ori - 1 # Don't count EOS
        tgt_seq = tgt_seq_ori[:, :-1]
        tgt_pos = tgt_pos[:, :-1]
        
        lengths_seq_src, idx_src = src_pos.max(1)

        tgt_lang_seq_forEnc = tgt_lang_seq if self.enc_lang else None
        src_lang_oneHot_forEnc = self.lang2oneHot(src_lang_seq, self.srcLangIdx2oneHotIdx) if self.enc_srcLang_oh else None
        tgt_lang_oneHot_forEnc = self.lang2oneHot(tgt_lang_seq, self.tgtLangIdx2oneHotIdx) if self.enc_tgtLang_oh else None

        enc_output = self.encoder(src_seq, lengths_seq_src, l_in=tgt_lang_seq_forEnc,
            src_lang_oneHot=src_lang_oneHot_forEnc, tgt_lang_oneHot=tgt_lang_oneHot_forEnc)
        

        tgt_lang_seq_forDec = tgt_lang_seq if self.dec_lang else None
        src_lang_oneHot_forDec = self.lang2oneHot(src_lang_seq, self.srcLangIdx2oneHotIdx) if self.dec_srcLang_oh else None
        tgt_lang_oneHot_forDec = self.lang2oneHot(tgt_lang_seq, self.tgtLangIdx2oneHotIdx) if self.dec_tgtLang_oh else None
        
        dec_output = self.decoder(enc_output, lengths_seq_src, tgt_seq, l_in=tgt_lang_seq_forDec,
            src_lang_oneHot=src_lang_oneHot_forDec, tgt_lang_oneHot=tgt_lang_oneHot_forDec)

        return dec_output

