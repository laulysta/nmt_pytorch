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

def get_loss(crit, pred, gold, opt, smoothing_eps=0.1):
    ''' Apply label smoothing if needed '''

    gold = gold.contiguous().view(-1)
    
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
    def __init__(self, model, crit, opt, disc=None):
        super(MainModel, self).__init__()
        self.model = model
        self.crit = crit
        self.opt = opt
        self.disc = disc

    def forward(self, src, tgt, tgt_lang=None):
        gold = tgt[0][:, 1:]
        
        if tgt_lang:
            pred, enc_avg = self.model(src, tgt, tgt_lang)
        else:
            pred, enc_avg = self.model(src, tgt)

        if self.opt.smoothing:
            pred = F.log_softmax(pred, dim=1)

        loss = get_loss(self.crit, pred, gold, self.opt)
        if self.disc:
            enc_log_probs = self.disc.forward(enc_avg) # bs x n_langs
            neg_entropy = torch.sum(enc_log_probs * torch.exp(enc_log_probs))
            loss = (loss, self.opt.gan_gen_coeff * neg_entropy)
            return loss, pred, enc_log_probs

        return loss, pred, None


def train_epoch(model, training_data, validation_data, validation_data_translate, crit, optimizer, opt, epoch_i, best_BLEU, nb_examples_seen, pct_next_save,
        disc=None, disc_crit=None, disc_optimizer=None):
    ''' Epoch operation in training phase'''

    model.train()

    total_loss = 0
    n_total_words = 0
    n_total_correct = 0

    total_gen_loss = 0
    total_disc_loss = 0

    nb_examples_save = training_data.nb_examples*pct_next_save
    for batch in tqdm(
            training_data, mininterval=2,
            desc='  - (Training)   ', leave=False):
        #print(training_data._starts)
        # prepare data

        if opt.target_lang and training_data._src_langs:
            src, tgt, src_lang, tgt_lang = batch
        elif opt.target_lang:
            src, tgt, tgt_lang = batch
        elif training_data._src_langs:
            src, tgt, src_lang = batch
        else:
            src, tgt = batch

        # forward
        optimizer.zero_grad()
        if opt.target_lang:
            loss, pred, enc_log_probs = model(src, tgt, tgt_lang)
        else:
            loss, pred, enc_log_probs = model(src, tgt)

        if opt.gan:
            ce_loss, gen_loss = loss
            loss = ce_loss + opt.gan_gen_coeff * gen_loss
        else:
            ce_loss = loss

        if opt.multi_gpu:
            loss.backward(torch.ones_like(loss.data), retain_graph=True)
        else:
            loss.backward(retain_graph=True) # Retain graph for disc_optimizer.backward

        # `clip_grad_norm` helps prevent the exploding gradient problem in RNNs/LSTMs.
        nn.utils.clip_grad_norm(model.parameters(), 1.0)

        # update parameters
        #print('G: ', sum(param.sum() for param in model.model.parameters()))
        optimizer.step()
        if opt.sch_optim:
            optimizer.update_learning_rate()

        if opt.gan:
            disc_optimizer.zero_grad()
            disc_loss = disc_crit(enc_log_probs, src_lang[0][:,0])
            disc_loss.backward()
            #print(disc_loss)
            #print('D: ', sum(param.sum() for param in disc.parameters()))
            disc_optimizer.step()
            if opt.sch_optim:
                disc_optimizer.update_learning_rate()

        nb_examples_seen += len(src[0]) # batch size
        if opt.save_model and nb_examples_seen >= nb_examples_save:
            pct_next_save += opt.save_freq_pct
            nb_examples_save = training_data.nb_examples*pct_next_save
            epoch_i += opt.save_freq_pct
            best_BLEU = save_model_and_validation_BLEU(opt, model, optimizer, validation_data, validation_data_translate, epoch_i, best_BLEU,
                    disc=disc, disc_optimizer=disc_optimizer)
            model.train()

        # note keeping
        gold = tgt[0][:, 1:]
        n_correct = get_performance(pred, gold)
        n_words = gold.data.ne(Constants.PAD).sum()
        n_total_words += n_words
        n_total_correct += n_correct
        total_loss += ce_loss.data[0]

        if opt.gan:
            total_gen_loss += gen_loss.data[0]
            total_disc_loss += disc_loss.data[0]

    return total_loss/n_total_words, n_total_correct/n_total_words, epoch_i, best_BLEU, nb_examples_seen, pct_next_save, \
            total_gen_loss/nb_examples_seen, total_disc_loss/nb_examples_seen

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
        if opt.target_lang:
            src, tgt, tgt_lang = batch
        else:
            src, tgt = batch
        gold = tgt[0][:, 1:]

        # forward
        if opt.target_lang:
            loss, pred, _ = model(src, tgt, tgt_lang)
        else:
            loss, pred, _ = model(src, tgt)

        # note keeping
        n_correct = get_performance(pred, gold)
        n_words = gold.data.ne(Constants.PAD).sum()
        n_total_words += n_words
        n_total_correct += n_correct
        if opt.gan:
            total_loss += loss[0].data[0]
        else:
            total_loss += loss.data[0]

    return total_loss/n_total_words, n_total_correct/n_total_words

def train(model, training_data, validation_data, validation_data_translate, crit, optimizer, opt, epoch_i, best_BLEU,
     disc=None, disc_crit=None, disc_optimizer=None):
    ''' Start training '''

    nb_examples_seen = 0
    pct_next_save = opt.save_freq_pct
    p_validation = None
    valid_accus = []
    for ii in range(opt.epoch):
        print('[ Starting epoch', epoch_i+1, ']')

        start = time.time()
        train_loss, train_accu, epoch_i, best_BLEU, nb_examples_seen, pct_next_save, gen_loss, disc_loss = train_epoch(model, training_data, validation_data,
                                                                                        validation_data_translate, crit, optimizer, opt,
                                                                                        epoch_i, best_BLEU, nb_examples_seen, pct_next_save,
                                                                                        disc=disc, disc_crit=disc_crit, disc_optimizer=disc_optimizer)
        if opt.gan:
            print('  - (Training)   ppl: {ppl: 8.5f}, accuracy: {accu:3.3f} %, exp(gen loss): {exp_gen_loss:8.5F}, disc ppl: {disc_ppl:8.5F}, '\
              'elapse: {elapse:3.3f} min'.format(
                  ppl=math.exp(min(train_loss, 100)), accu=100*train_accu,
                  exp_gen_loss=math.exp(min(gen_loss, 100)),
                  disc_ppl=math.exp(min(disc_loss, 100)),
                  elapse=(time.time()-start)/60))
        else:
            print('  - (Training)   ppl: {ppl: 8.5f}, accuracy: {accu:3.3f} %, '\
              'elapse: {elapse:3.3f} min'.format(
                  ppl=math.exp(min(train_loss, 100)), accu=100*train_accu,
                  elapse=(time.time()-start)/60))

        start = time.time()
        valid_loss, valid_accu = eval_epoch(model, validation_data, crit, opt)
        print('  - (Validation) ppl: {ppl: 8.5f}, accuracy: {accu:3.3f} %, '\
                'elapse: {elapse:3.3f} min'.format(
                    ppl=math.exp(min(valid_loss, 100)), accu=100*valid_accu,
                    elapse=(time.time()-start)/60))

        valid_accus += [valid_accu]


def save_model_and_validation_BLEU(opt, model, optimizer, validation_data, validation_data_translate, epoch_i, best_BLEU, valid_accu=None, valid_accus=None,
        disc=None, disc_optimizer=None):
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
    with open(output_name, 'w') as f:
        for batch in tqdm(validation_data_translate, mininterval=2, desc='  - (Translate and BLEU)', leave=False):
            #import ipdb; ipdb.set_trace()
            if opt.target_lang:
                (src_seq, src_pos), (tgt_lang_seq, tgt_lang_pos) = batch
            else:
                src_seq, src_pos  = batch
            
            lengths_seq_src, idx_src = src_pos.max(1)

            _, sent_sort_idx = lengths_seq_src.sort(descending=True)

            if opt.enc_lang:
                enc_output = model_translate.encoder(src_seq[sent_sort_idx], lengths_seq_src[sent_sort_idx], tgt_lang_seq[sent_sort_idx])
            else:
                enc_output = model_translate.encoder(src_seq[sent_sort_idx], lengths_seq_src[sent_sort_idx])

            if opt.dec_lang:
                all_hyp = model_translate.decoder.greedy_search(enc_output, lengths_seq_src[sent_sort_idx], tgt_lang_seq[sent_sort_idx])
            else:
                all_hyp = model_translate.decoder.greedy_search(enc_output, lengths_seq_src[sent_sort_idx])

            _, sent_revert_idx = sent_sort_idx.sort()
            sent_revert_idx = sent_revert_idx.data.view(-1).tolist()
            all_hyp = np.array(all_hyp)
            all_hyp = all_hyp[sent_revert_idx]

            #import ipdb; ipdb.set_trace()
            for idx_seq in all_hyp:
                if len(idx_seq) > 0: 
                    if idx_seq[-1] == Constants.EOS: # if last word is EOS
                        idx_seq = idx_seq[:-1]
                    pred_line = ' '.join([validation_data.tgt_idx2word[idx] for idx in idx_seq])
                    f.write(pred_line + '\n')
                else:
                    f.write('\n')
    try:
        #out = subprocess.check_output("perl multi-bleu.perl data/multi30k/val.de.atok < trained_epoch0_accu31.219.chkpt.output.dev", shell=True)
        out = subprocess.check_output("perl multi-bleu.perl " + opt.valid_bleu_ref + " < " + output_name, shell=True)
        out = out.decode() # because out is binary
        valid_BLEU = float(out.strip().split()[2][:-1])
    except:
        out = "multi-bleu.perl error"
        valid_BLEU = -1.0

    print(out)
    bleu_file = os.path.dirname(model_name) + '/bleu_scores.txt'
    with open(bleu_file, 'a') as f:
        #f.write("Epoch "+str(epoch_i)+": "+out)
        f.write("Epoch{epoch:3.2f} : {out}".format(epoch=epoch_i, out=out))
    
    ###########################################################################################################################

    if opt.multi_gpu:
        model_state_dict = model.module.model.state_dict()
    else:
        model_state_dict = model.model.state_dict()
    optimizer_state_dict = optimizer.state_dict()
    if opt.gan:
        disc_state_dict = disc.state_dict()
        disc_optimizer_state_dict = disc_optimizer.state_dict()
        checkpoint = {
            'model': model_state_dict,
            'optimizer': optimizer_state_dict,
            'settings': opt,
            'epoch': epoch_i,
            'best_BLEU': valid_BLEU if valid_BLEU >= best_BLEU else best_BLEU,
            'disc': disc_state_dict,
            'disc_optimizer': disc_optimizer_state_dict}
    else:
        checkpoint = {
            'model': model_state_dict,
            'optimizer': optimizer_state_dict,
            'settings': opt,
            'epoch': epoch_i,
            'best_BLEU': valid_BLEU if valid_BLEU >= best_BLEU else best_BLEU}

    torch.save(checkpoint, opt.save_model + '_tmp.chkpt')
    _ = subprocess.check_output('mv ' + opt.save_model + '_tmp.chkpt' + ' ' + opt.save_model + '.chkpt', shell=True)
    if opt.save_mode == 'all':
        #model_name = opt.save_model + '_epoch{epoch:3.2f}.chkpt'.format(epoch=epoch_i)
        torch.save(checkpoint, model_name)
    elif opt.save_mode == 'best':
        #model_name = opt.save_model + '.chkpt'
        if valid_BLEU >= best_BLEU:
            best_BLEU = valid_BLEU
            torch.save(checkpoint, opt.save_model + '_best_tmp.chkpt')
            _ = subprocess.check_output('mv ' + opt.save_model + '_best_tmp.chkpt' + ' ' + opt.save_model + '_best.chkpt', shell=True)
            print('    - [Info] The checkpoint file has been updated.')
    return best_BLEU

def load_model(opt):

    checkpoint = torch.load(opt.reload_model+'.chkpt' if opt.reload_model else opt.save_model+'.chkpt')
    model_opt = checkpoint['settings']
    epoch_i = checkpoint['epoch']
    best_BLEU = checkpoint['best_BLEU'] if 'best_BLEU' in checkpoint else -1.0
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
        share_enc_dec=model_opt.share_enc_dec,
        part_id=model_opt.part_id,
        enc_lang= model_opt.enc_lang,
        dec_lang=model_opt.dec_lang,
        cuda=opt.cuda)

    modelRNN.load_state_dict(checkpoint['model'])

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

    if not opt.gan:
        return modelRNN, optimizer, epoch_i, best_BLEU
    else:
        module_list = []
        for jj, in_dim in enumerate(opt.gan_dims[:-1]):
            if opt.gan_dropout:
                module_list.append(nn.Dropout(p=opt.gan_dropout))
            module_list.append(nn.Linear(in_dim, opt.gan_dims[jj+1]))
            module_list.append(nn.Tanh()) if jj < len(opt.gan_dims) - 2 else module_list.append(nn.LogSoftmax())
        disc = nn.Sequential(*module_list)

        disc.load_state_dict(checkpoint['disc'])

        if opt.optim == 'adadelta':
            disc_optimizer = optim.Adadelta(disc.parameters(),
                                        lr=1.0, rho=0.95, eps=1e-06, weight_decay=0)
        elif opt.optim == 'adam':
            disc_optimizer = optim.Adam(disc.parameters(),
                                    lr=opt.lr, betas=(0.9, 0.98), eps=1e-09)
        else:
            sys.exit('Wrong optimizer')

        if not opt.no_reload_optimizer:
            disc_optimizer.load_state_dict(checkpoint['disc_optimizer'])

        return modelRNN, optimizer, epoch_i, best_BLEU, disc, disc_optimizer

def main():
    ''' Main function '''
    parser = argparse.ArgumentParser()

    parser.add_argument('-data', required=True)

    parser.add_argument('-epoch', type=int, default=100)
    parser.add_argument('-batch_size', type=int, default=64)

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

    parser.add_argument('-sch_optim', action='store_true')

    parser.add_argument('-lr', type=float, default=0.001)

    parser.add_argument('-no_reload_optimizer', action='store_true')

    parser.add_argument('-no_reload', action='store_true')

    parser.add_argument('-reload_model', required=False, default='')

    parser.add_argument('-valid_bleu_ref', type=str, default='',
                        help='Path to the reference')

    parser.add_argument('-external_validation_script', type=str, default=None, metavar='PATH', nargs='*',
                         help="location of validation script (to run your favorite metric for validation) (default: %(default)s)")

    parser.add_argument('-share_enc_dec', action='store_true')

    parser.add_argument('-enc_lang', action='store_true')
    parser.add_argument('-dec_lang', action='store_true')

    parser.add_argument('-part_id', action='store_true')

    parser.add_argument('-balanced_data', action='store_true')

    parser.add_argument('-gan', action='store_true')
    parser.add_argument('-gan_dropout', type=float, default=0.)
    parser.add_argument('-gan_dims', type=int, nargs='*',
            help='All discriminator dimensions (including input and output)')
    parser.add_argument('-gan_gen_coeff', type=float, default=1.)
    opt = parser.parse_args()

    if opt.save_freq_pct <= 0.0 or opt.save_freq_pct > 1.0:
        raise argparse.ArgumentTypeError("-save_freq_pct: %r not in range [0.0, 1.0]"%(opt.save_freq_pct,))
    opt.cuda = not opt.no_cuda
    #opt.d_word_vec = opt.d_model

    opt.target_lang = True if opt.enc_lang or opt.dec_lang else False

    #========= Loading Dataset =========#
    data = torch.load(opt.data)
    opt.max_token_seq_len = data['settings'].max_token_seq_len

    #========= Preparing DataLoader =========#
    TrainDataLoader = DataLoaderMulti if opt.balanced_data else DataLoader
    
    training_data = TrainDataLoader(
        data['dict']['src'],
        data['dict']['tgt'],
        src_insts=data['train']['src'],
        tgt_insts=data['train']['tgt'],
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
        tgt_lang_insts=(data['valid']['tgt_lang'] if opt.target_lang else None),
        batch_size=opt.batch_size,
        shuffle=False,
        cuda=opt.cuda,
        is_train=False,
        sort_by_length=True)

    validation_data_translate = DataLoader(
        data['dict']['src'],
        data['dict']['tgt'],
        src_insts=data['valid']['src'],
        tgt_insts=None,
        tgt_lang_insts=(data['valid']['tgt_lang'] if opt.target_lang else None),
        batch_size=opt.batch_size,
        shuffle=False,
        cuda=opt.cuda,
        is_train=False,
        sort_by_length=False)

    opt.src_vocab_size = training_data.src_vocab_size
    opt.tgt_vocab_size = training_data.tgt_vocab_size


    #========= Preparing Model =========#
    if opt.embs_share_weight and training_data.src_word2idx != training_data.tgt_word2idx:
        print('[Warning]',
              'The src/tgt word2idx table are different but asked to share word embedding.')

    print(opt)

    #Create path if needed
    pathlib.Path(opt.save_model).parent.mkdir(parents=True, exist_ok=True)

    if not opt.no_reload and (os.path.isfile(opt.save_model+".chkpt") or os.path.isfile(opt.reload_model+".chkpt")):
        if opt.gan:
            modelRNN, optimizer, epoch_i, best_BLEU, disc, disc_optimizer = load_model(opt)
        else:
            modelRNN, optimizer, epoch_i, best_BLEU = load_model(opt)
    else:
        #Create model
        epoch_i = 0.0
        best_BLEU = -1.0

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
            share_enc_dec=opt.share_enc_dec,
            part_id=opt.part_id,
            enc_lang=opt.enc_lang,
            dec_lang=opt.dec_lang,
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

        if opt.gan:
            module_list = []
            for jj, in_dim in enumerate(opt.gan_dims[:-1]):
                if opt.gan_dropout:
                    module_list.append(nn.Dropout(p=opt.gan_dropout))
                module_list.append(nn.Linear(in_dim, opt.gan_dims[jj+1]))
                module_list.append(nn.Tanh()) if jj < len(opt.gan_dims) - 2 else module_list.append(nn.LogSoftmax())
            disc = nn.Sequential(*module_list)

            if opt.optim == 'adadelta':
                disc_optimizer = optim.Adadelta(disc.parameters(),
                                            lr=1.0, rho=0.95, eps=1e-06, weight_decay=0)
            elif opt.optim == 'adam':
                disc_optimizer = optim.Adam(disc.parameters(),
                                        lr=opt.lr, betas=(0.9, 0.98), eps=1e-09)
            else:
                sys.exit('Wrong optimizer')

            if opt.sch_optim:
                disc_optimizer = ScheduledOptim(disc_optimizer, opt.d_model, opt.n_warmup_steps)

    def get_criterion(vocab_size):
        ''' With PAD token zero weight '''
        weight = torch.ones(vocab_size)
        weight[Constants.PAD] = 0
        if opt.smoothing:
            return nn.NLLLoss(weight, size_average=False, ignore_index=Constants.PAD)
        else:
            return nn.CrossEntropyLoss(weight, size_average=False, ignore_index=Constants.PAD)

    crit = get_criterion(training_data.tgt_vocab_size)
    if opt.gan:
        disc_crit = nn.NLLLoss(size_average=False)
    if opt.cuda:
        modelRNN = modelRNN.cuda()
        crit = crit.cuda()
        if opt.gan:
            disc = disc.cuda()
            disc_crit = disc_crit.cuda()

    model = MainModel(modelRNN, crit, opt, disc if disc else None)
    if opt.multi_gpu:
        model = nn.DataParallel(model)

    train(model, training_data, validation_data, validation_data_translate, crit, optimizer, opt, epoch_i, best_BLEU,
         disc=disc if disc else None,
         disc_crit=disc_crit if disc_crit else None,
         disc_optimizer=disc_optimizer if disc_optimizer else None)

if __name__ == '__main__':
    main()
