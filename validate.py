import argparse
from argparse import Namespace
import sys, os
import os.path
import pathlib

import numpy as np
from subprocess import Popen
from tqdm import tqdm
import torch
import torch.nn as nn
import torch.nn.functional as F
import NMTmodelRNN.Constants as Constants
from NMTmodelRNN.Models import NMTmodelRNN
from NMTmodelRNN.Optim import ScheduledOptim
from DataLoader import DataLoader

from torch.autograd import Variable
import subprocess


def load_model(opt):

    checkpoint = torch.load(opt.model_name)
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
        cuda=opt.cuda)

    modelRNN.load_state_dict(checkpoint['model'])

    return modelRNN, epoch_i, best_BLEU, model_opt


def main():
    ''' Main function '''
    parser = argparse.ArgumentParser()

    parser.add_argument('-data', required=True)

    parser.add_argument('-model_name', required=True)

    parser.add_argument('-pred_path', type=str, default='resultTranslation',
                        help='Path to the output translation')

    parser.add_argument('-ref', type=str, default='',
                        help='Path to the reference')

    parser.add_argument('-batch_size', type=int, default=64)
    parser.add_argument('-no_cuda', action='store_true')
    parser.add_argument('-multi_gpu', action='store_true')

    opt = parser.parse_args()
    
    opt.cuda = not opt.no_cuda
    

    #import ipdb; ipdb.set_trace()
    #Create path if needed
    output_name = opt.pred_path + '/translation.output.txt'
    pathlib.Path(output_name).parent.mkdir(parents=True, exist_ok=True)

    #========= Loading Model =========#
    model_translate, epoch_i, best_BLEU, model_opt = load_model(opt)


    #========= Loading Dataset =========#
    data = torch.load(opt.data)

    data_set = DataLoader(
        data['dict']['src'],
        data['dict']['tgt'],
        src_insts=data['valid']['src'],
        tgt_insts=None,
        tgt_lang_insts=(data['valid']['tgt_lang'] if model_opt.target_lang else None),
        batch_size=opt.batch_size,
        shuffle=False,
        cuda=opt.cuda,
        is_train=False,
        sort_by_length=False)

    ###########################################################################################
    with open(output_name, 'w') as f:
        for batch in tqdm(data_set, mininterval=2, desc='  - (Translate and BLEU)', leave=False):
            #import ipdb; ipdb.set_trace()
            if model_opt.target_lang:
                (src_seq, src_pos), (tgt_lang_seq, tgt_lang_pos) = batch
            else:
                src_seq, src_pos  = batch
            
            lengths_seq_src, idx_src = src_pos.max(1)

            _, sent_sort_idx = lengths_seq_src.sort(descending=True)

            if model_opt.target_lang:
                enc_output = model_translate.encoder(src_seq[sent_sort_idx], lengths_seq_src[sent_sort_idx], tgt_lang_seq[sent_sort_idx])
            else:
                enc_output = model_translate.encoder(src_seq[sent_sort_idx], lengths_seq_src[sent_sort_idx])
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
                    pred_line = ' '.join([data_set.tgt_idx2word[idx] for idx in idx_seq])
                    f.write(pred_line + '\n')
                else:
                    f.write('\n')
    try:
        #out = subprocess.check_output("perl multi-bleu.perl data/multi30k/val.de.atok < trained_epoch0_accu31.219.chkpt.output.dev", shell=True)
        out = subprocess.check_output("perl multi-bleu.perl " + opt.ref + " < " + output_name, shell=True)
        out = out.decode() # because out is binary
        valid_BLEU = float(out.strip().split()[2][:-1])
    except:
        out = "multi-bleu.perl error"
        valid_BLEU = -1.0

    #print(out)
    #import ipdb; ipdb.set_trace()
    bleu_file = os.path.dirname(output_name) + '/bleu_scores.txt'
    with open(bleu_file, 'a') as f:
        #f.write("Epoch "+str(epoch_i)+": "+out)
        f.write("Result : {out}".format(out=out))
    print("Result : {out}".format(out=out))
    print('Best BLEU valid (greedy, epoch{epoch:3.2f}): {best_BLEU}'.format(epoch=epoch_i, best_BLEU=best_BLEU))


if __name__ == '__main__':
    main()
