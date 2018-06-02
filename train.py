'''
This script handling the training process.
'''

import argparse
from argparse import Namespace
import math
import time
import sys, os
import os.path
import pathlib

import numpy as np
from subprocess import Popen
from tqdm import tqdm
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import NMTmodelRNN.Constants as Constants
from NMTmodelRNN.Models import NMTmodelRNN
from NMTmodelRNN.Optim import ScheduledOptim
from DataLoader import DataLoader
from DataLoaderMulti import DataLoaderMulti
#from NMTmodelRNN.Translator import Translator
from torch.autograd import Variable
import subprocess

from validate import prepare_data, translate_data
import pickle

def get_loss(crit, pred, gold, opt, smoothing_eps=0.1):
    ''' Apply label smoothing if needed '''

    gold = gold.contiguous().view(-1)

    #import ipdb;ipdb.set_trace()
    if opt.simple_dist_precision > 0:
        topk_pred = pred.data.topk(opt.simple_dist_precision)[1]
        #mask_sd = torch.zeros_like(gold.data)
        #for ii in range(opt.simple_dist_precision):
        #    mask_sd += (gold.data == topk_pred[:,ii]).long()
        gold_rep = (gold.data.repeat(opt.simple_dist_precision,1).transpose(1,0))
        mask_sd = (gold_rep == topk_pred).sum(1).long()

        gold.data = gold.data * mask_sd

    loss = crit(pred, gold)

    if opt.smoothing and smoothing_eps:
        if opt.cuda:
            smooth = gold.ne(Constants.PAD).type(torch.cuda.FloatTensor) * torch.mean(pred, -1)
        else:
            smooth = gold.ne(Constants.PAD).type(torch.FloatTensor)*torch.mean(pred, -1)
        smooth = -smooth.sum()
        loss = (1-smoothing_eps)*loss + smoothing_eps*smooth
    return loss

def get_performance(pred, gold):
    gold = gold.contiguous().view(-1)
    pred = pred.max(1)[1]
    n_correct = pred.data.eq(gold.data)
    n_correct = n_correct.masked_select(gold.ne(Constants.PAD).data).sum()

    return n_correct

#g_n_correct = 0
class MainModel(nn.Module):
    def __init__(self, model, crit, opt):
        super(MainModel, self).__init__()
        self.model = model
        self.crit = crit
        self.opt = opt

    def forward(self, src, tgt, tgt_lang=None, src_lang=None):
        gold = tgt[0][:, 1:]

        pred = self.model(src, tgt, tgt_lang=tgt_lang, src_lang=src_lang)

        if self.opt.smoothing:
            pred = F.log_softmax(pred, dim=1)

        loss = get_loss(self.crit, pred, gold, self.opt)

        return loss, pred


def train_epoch(model, training_data, crit, optimizer, opt, epoch_i, best_BLEU, patience_count, nb_examples_seen, pct_next_save):
    ''' Epoch operation in training phase'''

    start = time.time()

    model.train()

    total_loss = 0
    n_total_src_words = 0
    n_total_words = 0
    n_total_correct = 0

    total_sim_loss = 0
    n_total_sim = 0

    init_nb_examples_seen = nb_examples_seen

    nb_examples_save = training_data.nb_examples*pct_next_save
    for batch in tqdm(
            training_data, mininterval=2,
            desc='  - (Training)   ', leave=False):
        #print(training_data._starts)
        # prepare data

        if opt.target_lang and opt.source_lang:
            src, tgt, src_lang, tgt_lang = batch
        elif opt.target_lang:
            src, tgt, tgt_lang = batch
        elif opt.source_lang:
            src, tgt, src_lang = batch
        else:
            src, tgt = batch

        # forward
        optimizer.zero_grad()

        loss, pred = model(src, tgt,
                tgt_lang=tgt_lang if opt.target_lang else None,
                src_lang=src_lang if opt.source_lang else None)

        if opt.multi_gpu:
            loss.backward(torch.ones_like(loss.data))
        else:
            loss.backward()

        # `clip_grad_norm` helps prevent the exploding gradient problem in RNNs/LSTMs.
        nn.utils.clip_grad_norm(model.parameters(), 1.0)

        # update parameters
        #print('G: ', sum(param.sum() for param in model.model.parameters()))
        optimizer.step()
        if opt.sch_optim:
            optimizer.update_learning_rate()

        # note keeping
        gold = tgt[0][:, 1:]
        n_correct = get_performance(pred, gold)
        n_words = gold.data.ne(Constants.PAD).sum()
        n_total_words += n_words
        n_total_correct += n_correct
        total_loss += loss.data[0]

        n_total_src_words += src[0].data.ne(Constants.PAD).sum()


        nb_examples_seen += len(src[0]) # batch size
        if opt.save_model and nb_examples_seen >= nb_examples_save:
            print('  - (Training)   ppl: {ppl: 8.5f}, accuracy: {accu:3.3f} %, '\
              'elapse: {elapse:3.3f} min'.format(
                  ppl=math.exp(min(total_loss/n_total_words, 100)), accu=100*n_total_correct/n_total_words,
                  elapse=(time.time()-start)/60))

            pct_next_save += opt.save_freq_pct
            nb_examples_save = training_data.nb_examples*pct_next_save
            epoch_i += opt.save_freq_pct
            best_BLEU, patience_count, optimizer = save_model_and_validation_BLEU(opt, model, optimizer, training_data.src_word2idx, training_data.tgt_word2idx, epoch_i, best_BLEU, patience_count)

            model.train()

    return total_loss/n_total_words, n_total_correct/n_total_words, epoch_i, best_BLEU, patience_count, optimizer, nb_examples_seen, pct_next_save

def eval_epoch(model, validation_data, crit, opt):
    ''' Epoch operation in evaluation phase '''

    model.eval()

    total_loss = 0
    n_total_words = 0
    n_total_correct = 0

    for batch in tqdm(
            validation_data, mininterval=2,
            desc='  - (Validation) ', leave=False):

        # prepare data
        # tgt always used here
        if opt.target_lang and opt.source_lang:
            src, tgt, src_lang, tgt_lang = batch
        elif opt.target_lang:
            src, tgt, tgt_lang = batch
        elif opt.source_lang:
            src, tgt, src_lang = batch
        else:
            src, tgt = batch
        gold = tgt[0][:, 1:]

        # forward

        loss, pred = model(src, tgt, tgt_lang=tgt_lang if opt.target_lang else None, src_lang=src_lang if opt.source_lang else None)

        # note keeping
        n_correct = get_performance(pred, gold)
        n_words = gold.data.ne(Constants.PAD).sum()
        n_total_words += n_words
        n_total_correct += n_correct
        total_loss += loss.data[0]

    return total_loss/n_total_words, n_total_correct/n_total_words

def train(model, training_data, validation_data, crit, optimizer, opt, epoch_i, best_BLEU, patience_count):
    ''' Start training '''

    nb_examples_seen = 0
    pct_next_save = opt.save_freq_pct
    p_validation = None
    valid_accus = []
    for ii in range(opt.epoch):
        print('[ Starting epoch', epoch_i+1, ']')

        train_loss, train_accu, epoch_i, best_BLEU, patience_count, optimizer, nb_examples_seen, pct_next_save = train_epoch(model, training_data, crit, optimizer, opt,
                                                                                                            epoch_i, best_BLEU, patience_count, nb_examples_seen, pct_next_save)
        start = time.time()
        valid_loss, valid_accu = eval_epoch(model, validation_data, crit, opt)
        print('  - (Validation) ppl: {ppl: 8.5f}, accuracy: {accu:3.3f} %, '\
                'elapse: {elapse:3.3f} min'.format(
                    ppl=math.exp(min(valid_loss, 100)), accu=100*valid_accu,
                    elapse=(time.time()-start)/60))

        valid_accus += [valid_accu]


def save_model_and_validation_BLEU(opt, model, optimizer, src_word2idx, tgt_word2idx, epoch_i, best_BLEU, patience_count, valid_accu=None, valid_accus=None):
    print('[ Epoch', epoch_i, ']')
    model.eval()

    if opt.save_mode == 'all':
        model_name = opt.save_model + '_epoch{epoch:3.2f}.chkpt'.format(epoch=epoch_i)
    elif opt.save_mode == 'best':
        model_name = opt.save_model + '.chkpt'

    if opt.multi_gpu:
        model_translate = model.module.model
    else:
        model_translate = model.model

    output_name = model_name + '.output.dev'
    bleu_file = os.path.dirname(model_name) + '/bleu_scores.txt'

    ###########################################################################################################################
    if opt.extra_valid_src and opt.extra_valid_tgt: # and opt.extra_valid_tgtLang:
        assert len(opt.extra_valid_src) == len(opt.extra_valid_tgt)
        if opt.extra_valid_tgtLang:
            assert len(opt.extra_valid_src) == len(opt.extra_valid_tgtLang)
        else:
            opt.extra_valid_tgtLang = [None] * len(opt.extra_valid_src)

        if opt.extra_valid_srcLang:
            assert len(opt.extra_valid_src) == len(opt.extra_valid_srcLang)
        else:
            opt.extra_valid_srcLang = [None] * len(opt.extra_valid_src)

        with open(bleu_file, 'a') as f:
            f.write('Epoch: %.3f , lr: %f\n' % (epoch_i, opt.lr))

        for ii, (src_path, tgt_path, srcLang_path, tgtLang_path) in enumerate(zip(opt.extra_valid_src, opt.extra_valid_tgt, opt.extra_valid_srcLang, opt.extra_valid_tgtLang)):
            data_set = prepare_data(src_path, src_word2idx, tgt_word2idx, opt, srcLang_path=srcLang_path, tgtLang_path=tgtLang_path)
            #extra_bleu_file_name = os.path.dirname(model_name) + '/bleu_scores_extra' + str(ii+1) + '.txt'
            extra_output_name = output_name + '_extra' + str(ii+1)
            res = translate_data(model_translate, data_set, extra_output_name, opt, tgt_path, bleu_file_name=bleu_file)
            if ii == 0:
                valid_BLEU = res

    ###########################################################################################################################
    model_state_dict = model_translate.state_dict()
    optimizer_state_dict = optimizer.state_dict()
    checkpoint = {
        'model': model_state_dict,
        'optimizer': optimizer_state_dict,
        'settings': opt,
        'epoch': epoch_i,
        'best_BLEU': valid_BLEU if valid_BLEU >= best_BLEU else best_BLEU,
        'patience_count': patience_count}

    if valid_BLEU >= best_BLEU:
        best_BLEU = valid_BLEU
        torch.save(checkpoint, opt.save_model + '_best_tmp.chkpt')
        _ = subprocess.check_output('mv ' + opt.save_model + '_best_tmp.chkpt' + ' ' + opt.save_model + '_best.chkpt', shell=True)
        print('    - [Info] The checkpoint file has been updated.')
        patience_count = 0
    else:
        patience_count += 1

    if opt.optim == 'adam' and patience_count == opt.adam_patience:
        bestModelName = opt.save_model + '_best.chkpt'
        checkpoint = torch.load(bestModelName)
        model_translate.load_state_dict(checkpoint['model'])
        epoch_i -= patience_count * opt.save_freq_pct
        opt.lr = opt.lr/2
        optimizer = optim.Adam(model_translate.parameters(),
                        lr=opt.lr, betas=(0.9, 0.98), eps=1e-09)
        checkpoint['optimizer'] = optimizer.state_dict()
        checkpoint['patience_count'] = 0
        patience_count = 0

    ###########################################################################################################################
    
    torch.save(checkpoint, opt.save_model + '_tmp.chkpt')
    _ = subprocess.check_output('mv ' + opt.save_model + '_tmp.chkpt' + ' ' + opt.save_model + '.chkpt', shell=True)

    if opt.save_mode == 'all':
        #model_name = opt.save_model + '_epoch{epoch:3.2f}.chkpt'.format(epoch=epoch_i)
        torch.save(checkpoint, model_name)
    
    return best_BLEU, patience_count, optimizer

def load_model(opt):

    checkpoint = torch.load(opt.reload_model+'.chkpt' if opt.reload_model else opt.save_model+'.chkpt')
    model_opt = checkpoint['settings']
    epoch_i = checkpoint['epoch']
    best_BLEU = checkpoint['best_BLEU'] if 'best_BLEU' in checkpoint else -1.0
    patience_count = checkpoint['patience_count'] if 'patience_count' in checkpoint else 0
    modelRNN = NMTmodelRNN(
        model_opt.src_vocab_size,
        model_opt.tgt_vocab_size,
        model_opt.max_token_seq_len,
        no_proj_share_weight=model_opt.no_proj_share_weight,
        embs_share_weight=model_opt.embs_share_weight,
        d_model=model_opt.d_model,
        d_word_vec=model_opt.d_word_vec,
        n_layers=model_opt.n_layers,
        dropout=model_opt.dropout,
        enc_lang= model_opt.enc_lang,
        dec_lang=model_opt.dec_lang,
        enc_srcLang_oh=model_opt.enc_srcLang_oh,
        enc_tgtLang_oh=model_opt.enc_tgtLang_oh,
        dec_srcLang_oh=model_opt.dec_srcLang_oh,
        dec_tgtLang_oh=model_opt.dec_tgtLang_oh,
        srcLangIdx2oneHotIdx=model_opt.srcLangIdx2oneHotIdx,
        tgtLangIdx2oneHotIdx=model_opt.tgtLangIdx2oneHotIdx,
        share_bidir=model_opt.share_bidir,
        share_enc_dec=model_opt.share_enc_dec,
        share_dec_temp=model_opt.share_dec_temp,
        simple_dist_precision=opt.simple_dist_precision,
        cuda=opt.cuda)

    
    modelRNN.load_state_dict(checkpoint['model'])

    if not opt.no_reload_optimizer:
        opt.lr = model_opt.lr

    if opt.optim == 'adadelta':
        optimizer = optim.Adadelta(modelRNN.parameters(),
                                    lr=1.0, rho=0.95, eps=1e-06, weight_decay=0)
    elif opt.optim == 'adam':
        optimizer = optim.Adam(modelRNN.parameters(),
                                lr=opt.lr, betas=(0.9, 0.98), eps=1e-09)
    else:
        sys.exit('Wrong optimizer')

    if opt.sch_optim:
        optimizer = ScheduledOptim(optimizer, opt.d_model, opt.n_warmup_steps)

    if not opt.no_reload_optimizer:
        optimizer.load_state_dict(checkpoint['optimizer'])
    else:
        patience_count = 0

    return modelRNN, optimizer, epoch_i, best_BLEU, patience_count

def dict_lang(lang_data):
    lang_token_idx = set()
    for ii in lang_data:
        lang_token_idx.add(ii[0])
    list_lang_idx = list(lang_token_idx)
    list_lang_idx.sort()

    nb_lang = len(list_lang_idx)
    langIdx2oneHotIdx = {}
    for ii, idx in enumerate(list_lang_idx):
        langIdx2oneHotIdx[idx] = ii
    
    return langIdx2oneHotIdx

def nb_lang(lang_data):
    lang_token_idx = set()
    for ii in lang_data:
        lang_token_idx.add(ii[0])
    
    return len(lang_token_idx)

def main():
    ''' Main function '''
    parser = argparse.ArgumentParser()

    parser.add_argument('-data', required=True)

    parser.add_argument('-epoch', type=int, default=100)
    parser.add_argument('-batch_size', type=int, default=64)
    parser.add_argument('-valid_batch_size', type=int, default=32)

    parser.add_argument('-d_word_vec', type=int, default=620)
    parser.add_argument('-d_model', type=int, default=1000)

    parser.add_argument('-n_layers', type=int, default=1)
    parser.add_argument('-n_warmup_steps', type=int, default=4000)

    parser.add_argument('-dropout', type=float, default=0.5)
    parser.add_argument('-embs_share_weight', action='store_true')
    parser.add_argument('-no_proj_share_weight', action='store_true')
    parser.add_argument('-smoothing', action='store_true')


    parser.add_argument('-save_model', required=True)
    parser.add_argument('-save_mode', type=str, choices=['all', 'best'], default='best')
    parser.add_argument('-save_freq_pct', type=float, default=1.0)

    parser.add_argument('-no_cuda', action='store_true')

    parser.add_argument('-multi_gpu', action='store_true')

    parser.add_argument('-optim', type=str, choices=['adam', 'adadelta'], default='adam')

    parser.add_argument('-adam_patience', type=int, default=-1)

    parser.add_argument('-sch_optim', action='store_true')

    parser.add_argument('-lr', type=float, default=0.0001)

    parser.add_argument('-no_reload_optimizer', action='store_true')

    parser.add_argument('-no_reload', action='store_true')

    parser.add_argument('-reload_model', required=False, default='')

    parser.add_argument('-external_validation_script', type=str, default=None, metavar='PATH', nargs='*',
                         help="location of validation script (to run your favorite metric for validation) (default: %(default)s)")

    parser.add_argument('-enc_lang', action='store_true')
    parser.add_argument('-dec_lang', action='store_true')

    parser.add_argument('-enc_srcLang_oh', action='store_true')
    parser.add_argument('-enc_tgtLang_oh', action='store_true')
    parser.add_argument('-dec_srcLang_oh', action='store_true')
    parser.add_argument('-dec_tgtLang_oh', action='store_true')

    parser.add_argument('-balanced_data', action='store_true')

    parser.add_argument('-extra_valid_src', type=str, nargs='+', help='Path extra valid source')
    parser.add_argument('-extra_valid_tgt', type=str, nargs='+', help='Path extra valid target')
    parser.add_argument('-extra_valid_srcLang', type=str, nargs='+', help='Path extra valid source language')
    parser.add_argument('-extra_valid_tgtLang', type=str, nargs='+', help='Path extra valid target language')

    parser.add_argument('-share_bidir', action='store_true')
    parser.add_argument('-share_enc_dec', action='store_true')
    parser.add_argument('-share_dec_temp', action='store_true')

    parser.add_argument('-simple_dist_precision', type=int, default=0)

    opt = parser.parse_args()

    assert not (opt.share_bidir and opt.share_enc_dec)

    assert not (opt.dec_lang and opt.dec_tgtLang_oh)
    assert not (opt.enc_lang and opt.enc_tgtLang_oh)
    assert not (opt.enc_lang and opt.enc_srcLang_oh)

    if opt.save_freq_pct <= 0.0 or opt.save_freq_pct > 1.0:
        raise argparse.ArgumentTypeError("-save_freq_pct: %r not in range [0.0, 1.0]"%(opt.save_freq_pct,))
    opt.cuda = not opt.no_cuda
    #opt.d_word_vec = opt.d_model

    opt.target_lang = True if opt.enc_lang or opt.dec_lang or opt.dec_tgtLang_oh or opt.enc_tgtLang_oh else False
    opt.source_lang = True if opt.enc_srcLang_oh or opt.dec_srcLang_oh else False

    #========= Loading Dataset =========#
    if opt.data[-3:] == '.pt':
        data = torch.load(opt.data)
    elif opt.data[-4:] == '.pkl':
        with open(opt.data, 'rb') as f:
            data = pickle.load(f)
    else:
        sys.exit('Wrong data file')
    opt.max_token_seq_len = data['settings'].max_token_seq_len

    #========= Preparing DataLoader =========#
    TrainDataLoader = DataLoaderMulti if opt.balanced_data else DataLoader
    
    training_data = TrainDataLoader(
        data['dict']['src'],
        data['dict']['tgt'],
        src_insts=data['train']['src'],
        tgt_insts=data['train']['tgt'],
        src_lang_insts=(data['train']['src_lang'] if opt.source_lang else None),
        tgt_lang_insts=(data['train']['tgt_lang'] if opt.target_lang else None),
        batch_size=opt.batch_size,
        cuda=opt.cuda,
        is_train=True,
        sort_by_length=True)

    validation_data = DataLoader(
        data['dict']['src'],
        data['dict']['tgt'],
        src_insts=data['valid']['src'],
        tgt_insts=data['valid']['tgt'],
        src_lang_insts=(data['valid']['src_lang'] if opt.source_lang else None),
        tgt_lang_insts=(data['valid']['tgt_lang'] if opt.target_lang else None),
        batch_size=opt.valid_batch_size,
        shuffle=False,
        cuda=opt.cuda,
        is_train=False,
        sort_by_length=True)

    
    opt.src_vocab_size = training_data.src_vocab_size
    opt.tgt_vocab_size = training_data.tgt_vocab_size

    opt.tgtLangIdx2oneHotIdx = dict_lang(data['train']['tgt_lang']) if opt.dec_tgtLang_oh or opt.enc_tgtLang_oh else {}

    if opt.enc_srcLang_oh or opt.dec_srcLang_oh:
        nb_src_lang = nb_lang(data['train']['src_lang'])
        opt.srcLangIdx2oneHotIdx = {}
        for ii in range(nb_src_lang):
            opt.srcLangIdx2oneHotIdx[ii] = ii
    else:
        opt.srcLangIdx2oneHotIdx = {}

    #========= Preparing Model =========#
    if opt.embs_share_weight and training_data.src_word2idx != training_data.tgt_word2idx:
        print('[Warning]',
              'The src/tgt word2idx table are different but asked to share word embedding.')

    print(opt)

    #Create path if needed
    pathlib.Path(opt.save_model).parent.mkdir(parents=True, exist_ok=True)

    if not opt.no_reload and (os.path.isfile(opt.save_model+".chkpt") or os.path.isfile(opt.reload_model+".chkpt")):
        modelRNN, optimizer, epoch_i, best_BLEU, patience_count = load_model(opt)
    else:
        #Create model
        epoch_i = 0.0
        best_BLEU = -1.0
        patience_count = 0

        modelRNN = NMTmodelRNN(
            opt.src_vocab_size,
            opt.tgt_vocab_size,
            opt.max_token_seq_len,
            no_proj_share_weight=opt.no_proj_share_weight,
            embs_share_weight=opt.embs_share_weight,
            d_model=opt.d_model,
            d_word_vec=opt.d_word_vec,
            n_layers=opt.n_layers,
            dropout=opt.dropout,
            enc_lang=opt.enc_lang,
            dec_lang=opt.dec_lang,
            enc_srcLang_oh=opt.enc_srcLang_oh,
            enc_tgtLang_oh=opt.enc_tgtLang_oh,
            dec_srcLang_oh=opt.dec_srcLang_oh,
            dec_tgtLang_oh=opt.dec_tgtLang_oh,
            srcLangIdx2oneHotIdx=opt.srcLangIdx2oneHotIdx,
            tgtLangIdx2oneHotIdx=opt.tgtLangIdx2oneHotIdx,
            share_bidir=opt.share_bidir,
            share_enc_dec=opt.share_enc_dec,
            share_dec_temp=opt.share_dec_temp,
            simple_dist_precision=opt.simple_dist_precision,
            cuda=opt.cuda)

        #print(modelRNN)

        if opt.optim == 'adadelta':
            optimizer = optim.Adadelta(modelRNN.parameters(),
                                        lr=1.0, rho=0.95, eps=1e-06, weight_decay=0)
        elif opt.optim == 'adam':
            #optimizer = optim.Adam(modelRNN.get_trainable_parameters(),
            #                        lr=opt.lr, betas=(0.9, 0.98), eps=1e-09)
            optimizer = optim.Adam(modelRNN.parameters(),
                                    lr=opt.lr, betas=(0.9, 0.98), eps=1e-09)
        else:
            sys.exit('Wrong optimizer')

        if opt.sch_optim:
            optimizer = ScheduledOptim(optimizer, opt.d_model, opt.n_warmup_steps)


    def get_criterion(vocab_size):
        ''' With PAD token zero weight '''
        weight = torch.ones(vocab_size)
        weight[Constants.PAD] = 0
        if opt.smoothing:
            return nn.NLLLoss(weight, size_average=False, ignore_index=Constants.PAD)
        else:
            return nn.CrossEntropyLoss(weight, size_average=False, ignore_index=Constants.PAD)

    crit = get_criterion(training_data.tgt_vocab_size)
    
    if opt.cuda:
        modelRNN = modelRNN.cuda()
        crit = crit.cuda()

    model = MainModel(modelRNN, crit, opt)
    if opt.multi_gpu:
        model = nn.DataParallel(model)

    train(model, training_data, validation_data, crit, optimizer, opt, epoch_i, best_BLEU, patience_count)

if __name__ == '__main__':
    main()
