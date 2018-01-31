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

class Encoder(nn.Module):
    def __init__(self, n_src_vocab, n_max_seq, n_layers=2,
                d_word_vec=512, d_model=512, dropout=0.5, cuda=False):
        super(Encoder, self).__init__()
        self.tt = torch.cuda if cuda else torch
        d_ctx = d_model*2

        self.emb = nn.Embedding(n_src_vocab, d_word_vec, padding_idx=Constants.PAD)
        self.rnn = nn.GRU(
                    input_size=d_word_vec,
                    hidden_size=d_model,
                    num_layers=n_layers,
                    dropout=dropout,
                    batch_first=True,
                    bidirectional=True)
        self.drop = nn.Dropout(p=dropout)
        self.n_layers = n_layers
        self.d_model = d_model
        self.tt = torch.cuda if cuda else torch

    def forward(self, x_in, x_in_lens):
        # x_in : (batch_size, x_seq_len)
        # x_in_lens : (batch_size)
        batch_size, x_seq_len = x_in.size()

        h_0 = Variable( self.tt.FloatTensor(self.n_layers * 2, \
                                            batch_size, self.d_model).zero_() )
        x_in_emb = self.emb(x_in) # (batch_size, x_seq_len, D_emb)
        x_in_emb = self.drop(x_in_emb)

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

class EncoderShare(nn.Module):
    def __init__(self, n_src_vocab, n_max_seq, n_layers=1,
                d_word_vec=512, d_model=512, dropout=0.5, cuda=False):
        super(EncoderShare, self).__init__()
        self.tt = torch.cuda if cuda else torch
        d_ctx = d_model*2

        self.emb = nn.Embedding(n_src_vocab, d_word_vec, padding_idx=Constants.PAD)
        self.rnn = nn.GRU(
                    input_size=d_word_vec+d_ctx,
                    hidden_size=d_model,
                    num_layers=n_layers,
                    dropout=dropout,
                    batch_first=True,
                    bidirectional=False)
        self.drop = nn.Dropout(p=dropout)
        self.n_layers = n_layers
        self.d_model = d_model
        self.tt = torch.cuda if cuda else torch
        self.d_ctx = d_ctx

    def forward(self, x_in, x_in_lens):
        # x_in : (batch_size, x_seq_len)
        # x_in_lens : (batch_size)
        batch_size, x_seq_len = x_in.size()

        h_0 = Variable( self.tt.FloatTensor(self.n_layers, \
                                            batch_size, self.d_model).zero_() )
        x_in_emb = self.emb(x_in) # (batch_size, x_seq_len, D_emb)
        x_in_emb = self.drop(x_in_emb)

        pad_att = Variable( self.tt.FloatTensor(x_in_emb.size()[0], \
                                                x_in_emb.size()[1], self.d_ctx).zero_() )

        x_in_emb = torch.cat((x_in_emb, pad_att), dim=2) 

        # Lengths data is wrapped inside a Variable.
        x_in_lens = x_in_lens.data.view(-1).tolist()

        #Create reverse seq emb
        x_in_emb_r = torch.zeros_like(x_in_emb)
        for ii, seq_len in enumerate(x_in_lens):
            tmp = x_in_emb[ii,:seq_len,:]
            x_in_emb_r[ii,:seq_len,:] = flip(tmp, 0)

        pack = torch.nn.utils.rnn.pack_padded_sequence(x_in_emb, x_in_lens, batch_first=True)
        pack_r = torch.nn.utils.rnn.pack_padded_sequence(x_in_emb_r, x_in_lens, batch_first=True)
        
        # input (batch_size, x_seq_len, D_emb)
        # h_0 (num_layers * num_dir, batch_size, D_hid)
        top_layer, h_n = self.rnn(pack, h_0)
        top_layer_r, h_n_r = self.rnn(pack_r, h_0)
        # top_layer (batch_size, x_seq_len, D_hid * num_dir)
        # h_n (num_layers * num_dir, batch_size, D_hid)

        # attentional decoder : return the entire sequence of h_n
        out1, outlen1 = torch.nn.utils.rnn.pad_packed_sequence(top_layer, batch_first = True)
        out_r, outlen_r = torch.nn.utils.rnn.pad_packed_sequence(top_layer_r, batch_first = True)

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
             d_word_vec=512, d_model=512, dropout=0.5, proj_share_weight=True, cuda=False):
        super(Decoder, self).__init__()
        self.tt = torch.cuda if cuda else torch
        d_ctx = d_model*2

        self.emb = nn.Embedding(n_tgt_vocab, d_word_vec, padding_idx=0)
        #self.rnn = nn.GRUCell(d_ctx+d_word_vec, d_model)
        self.rnn = nn.GRU(d_ctx+d_word_vec, d_model, \
                           n_layers, dropout=dropout, batch_first=True)
        self.drop = nn.Dropout(p=dropout)
        self.ctx_to_s0 = nn.Linear(d_ctx, n_layers * d_model)

        self.y_to_ctx = nn.Linear(d_word_vec, d_ctx)
        self.s_to_ctx = nn.Linear(d_model * n_layers, d_ctx)
        self.h_to_ctx = nn.Linear(d_ctx, d_ctx)
        self.ctx_to_score = nn.Linear(d_ctx, 1)

        self.y_to_fin = nn.Linear(d_word_vec, d_word_vec)
        self.c_to_fin = nn.Linear(d_ctx, d_word_vec)
        self.s_to_fin = nn.Linear(d_model, d_word_vec)
        self.fin_to_voc = nn.Linear(d_word_vec, n_tgt_vocab, bias=False)

        if proj_share_weight:
            # Share the weight matrix between tgt word embedding/projection
            #assert d_model == d_word_vec
            self.emb.weight = self.fin_to_voc.weight

        self.n_layers = n_layers
        self.d_ctx = d_ctx
        self.d_model = d_model
        self.n_tgt_vocab = n_tgt_vocab
        self.d_word_vec = d_word_vec
        self.n_max_seq = n_max_seq

    def forward(self, h_in, h_in_len, y_in):
        # h_in : (batch_size, x_seq_len, d_ctx)
        # h_in_len : (batch_size)
        # y_in : (batch_size, y_seq_len)
        batch_size, y_seq_len = y_in.size()
        x_seq_len = h_in.size()[1]
        h_in_len = h_in_len.data.view(-1)
        xmask = xlen_to_mask_rnn(h_in_len.tolist(), self.tt) # (batch_size, x_seq_len)

        #import ipdb; ipdb.set_trace()

        s_0 = torch.sum(h_in, 1) # (batch_size, D_hid_enc * num_dir_enc)
        s_0 = torch.div( s_0, Variable(self.tt.FloatTensor(h_in_len.tolist()).view(-1,1)) )
        s_0 = self.ctx_to_s0(s_0)
        s_tm1 = s_0 # (batch_size, n_layers * d_model)
        s_tm1 = s_tm1.view(batch_size, self.n_layers, self.d_model).transpose(0,1).contiguous() \
                # (n_layers, batch_size, d_model)

        y_in_emb = self.emb(y_in) # (batch_size, y_seq_len, d_word_vec)
        y_in_emb = self.drop(y_in_emb) # (batch_size, y_seq_len, d_word_vec)

        h_in_big = h_in.view(-1, self.d_ctx) \
                # (batch_size * x_seq_len, d_ctx) 

        ctx_h = self.h_to_ctx( h_in_big ).view(batch_size, x_seq_len, self.d_ctx)
                # (batch_size, x_seq_len, d_ctx)

        logits = []
        for idx in range(y_seq_len):
            ctx_s_t_ = s_tm1.transpose(0,1).contiguous().view(batch_size, -1) \
                    # (batch_size, d_model * n_layers)

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
            # in (batch_size, 1, d_ctx)
            # s_t (n_layers, batch_size, d_model)
            out, s_t = self.rnn( torch.cat((y_in_emb[:,idx,:][:,None,:], c_t[:,None,:]), dim=2), s_tm1 )
            ####################################################
            #s_t = self.rnn( torch.cat((c_t, y_in_emb[:,idx,:]), dim=1), s_tm1[0] )
            #s_t = self.drop(s_t)
            #out = s_t[:,None,:]
            #s_t = s_t[None,:,:]
            ####################################################
            # out (batch_size, 1, d_model)
            # s_t (n_layers, batch_size, d_model)

            fin_y = self.y_to_fin( y_in_emb[:,idx,:] ) # (batch_size, d_word_vec)
            fin_c = self.c_to_fin( c_t ) # (batch_size, d_word_vec)
            fin_s = self.s_to_fin( out.view(-1, self.d_model) ) # (batch_size, d_word_vec)
            fin = F.tanh( fin_y + fin_c + fin_s )
            s_tm1 = s_t

            logit = self.fin_to_voc( fin ) # (batch_size, vocab_size)
            logits.append( logit )
            # logits : list of (batch_size, vocab_size) vectors

        ans = torch.cat(logits, 0) # (y_seq_len * batch_size, vocab_size)
        ans = ans.view(y_seq_len, batch_size, self.n_tgt_vocab).transpose(0,1).contiguous()
        return ans.view(batch_size * y_seq_len, -1)

    def greedy_search(self, h_in, h_in_len):
        # h_in : (batch_size, x_seq_len, d_ctx)
        # h_in_len : (batch_size)
        h_in_len = h_in_len.data.view(-1).tolist()
        #import ipdb; ipdb.set_trace()
        batch_size, x_seq_len = h_in.size()[0], h_in.size()[1]
        xmask = xlen_to_mask_rnn(h_in_len, self.tt) # (batch_size, x_seq_len)

        s_tm1 = torch.sum(h_in, 1) # (batch_size, d_ctx)
        s_tm1 = torch.div( s_tm1, Variable(self.tt.FloatTensor(h_in_len).view(batch_size, 1)) )
        s_tm1= self.ctx_to_s0(s_tm1)
        s_tm1 = s_tm1.view(batch_size, self.n_layers, self.d_model).transpose(0,1).contiguous() \
                # (n_layers, batch_size, d_model)

        start = [2 for ii in range(batch_size)] # NOTE <BOS> is always 2
        ft = self.tt.LongTensor(start)[:,None] # (batch_size, 1)
        y_in_emb = self.emb( Variable( ft ) ) # (batch_size, 1, d_word_vec)

        h_in_big = h_in.contiguous().view(batch_size * x_seq_len, self.d_ctx ) \
                # (batch_size * x_seq_len, D_hid_enc * num_dir_enc) 
        ctx_h = self.h_to_ctx( h_in_big ).view(batch_size, x_seq_len, self.d_ctx)

        gen_idx = [[] for ii in range(batch_size)]
        done = np.array( [False for ii in range(batch_size)] )

        for idx in range(self.n_max_seq):
            # in (batch_size, 1, d_word_vec)
            # s_t (n_layers, batch_size, d_model)
            #_, s_t_ = self.rnn1( y_in_emb, s_t )

            ctx_s_t_ = s_tm1.transpose(0,1).contiguous().view(batch_size, self.n_layers * self.d_model) \
                    # (batch_size, n_layers * d_model)

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
            # in (batch_size, 1, d_ctx)
            # s_t (n_layers, batch_size, d_model)
            out, s_t = self.rnn( torch.cat((c_t[:,None,:], y_in_emb[:,0,:][:,None,:]), dim=2), s_tm1 )
            ####################################################
            #s_t = self.rnn(torch.cat((c_t, y_in_emb[:,0,:]), dim=1), s_tm1[0] )
            #s_t = self.drop(s_t)
            #out = s_t[:,None,:]
            #s_t = s_t[None,:,:]
            ####################################################
            # out (batch_size, 1, d_model)
            # s_t (n_layers, batch_size, d_model)

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
            s_tm1 = s_t

        return gen_idx


#class NMTmodel(nn.Module):
class NMTmodelRNN(nn.Module):
    ''' A sequence to sequence model with attention mechanism. '''

    def __init__(
            self, n_src_vocab, n_tgt_vocab, n_max_seq, n_layers=2,
            d_word_vec=512, d_model=512,
            dropout=0.1, proj_share_weight=True, embs_share_weight=True, share_enc_dec=False, cuda=False):

        self.n_layers = n_layers

        super(NMTmodelRNN, self).__init__()

        self.decoder = Decoder(
            n_tgt_vocab, n_max_seq, n_layers=n_layers,
            d_word_vec=d_word_vec, d_model=d_model,
            dropout=dropout, proj_share_weight = proj_share_weight, cuda=cuda)

        if share_enc_dec:
            self.encoder = EncoderShare(n_src_vocab, n_max_seq, n_layers=n_layers,
                                    d_word_vec=d_word_vec, d_model=d_model,
                                    dropout=dropout, cuda=cuda)
            self.encoder.rnn = self.decoder.rnn
        else:
            self.encoder = Encoder(n_src_vocab, n_max_seq, n_layers=n_layers,
                                    d_word_vec=d_word_vec, d_model=d_model,
                                    dropout=dropout, cuda=cuda)

        if embs_share_weight:
            # Share the weight matrix between src/tgt word embeddings
            # assume the src/tgt word vec size are the same
            assert n_src_vocab == n_tgt_vocab, \
            "To share word embedding table, the vocabulary size of src/tgt shall be the same."
            self.encoder.emb.weight = self.decoder.emb.weight

    # def get_trainable_parameters(self):
    #     ''' Avoid updating the position encoding '''
    #     enc_freezed_param_ids = set(map(id, self.encoder.position_enc.parameters()))
    #     dec_freezed_param_ids = set(map(id, self.decoder.position_enc.parameters()))
    #     if (self.use_ctx or self.use_ctx_dep_src or self.use_gated_ctx) and not self.tie_encoders:
    #         ctx_freezed_param_ids = set(map(id, self.encoder_ctx.position_enc.parameters()))
    #         freezed_param_ids = enc_freezed_param_ids | dec_freezed_param_ids | ctx_freezed_param_ids
    #     else:
    #         freezed_param_ids = enc_freezed_param_ids | dec_freezed_param_ids
    #     return (p for p in self.parameters() if id(p) not in freezed_param_ids)

    def forward(self, src, tgt):
        # src_seq and src_pos: sent_len * batch_size
        src_seq, src_pos = src
        tgt_seq, tgt_pos = tgt

        tgt_seq = tgt_seq[:, :-1]
        tgt_pos = tgt_pos[:, :-1]
        
        lengths_seq_src, idx_src = src_pos.max(1)
        #lengths_seq_tgt, idx_tgt = tgt_pos.max(1)

        enc_output = self.encoder(src_seq, lengths_seq_src)
        
        dec_output = self.decoder(enc_output, lengths_seq_src, tgt_seq)

        #import ipdb; ipdb.set_trace()

        
        return dec_output
