''' Data Loader class for training iteration '''
import random
import numpy as np
import torch
from torch.autograd import Variable
import NMTmodelRNN.Constants as Constants

class DataLoaderMulti(object):
    ''' For data iteration '''

    def __init__(
            self, src_word2idx, tgt_word2idx,
            src_insts=None, tgt_insts=None, src_lang_insts=None, tgt_lang_insts=None,
            cuda=True, batch_size=64, shuffle=True,
            is_train=True, sort_by_length=False,
            maxibatch_size=20):

        assert src_insts
        assert tgt_lang_insts is not None
        assert len(src_insts) >= batch_size

        if tgt_insts:
            assert len(src_insts) == len(tgt_insts)

        #self.nb_examples = len(src_insts)
        self.cuda = cuda
        self._n_batch = int(np.ceil(len(src_insts) / batch_size))

        if self._n_batch % maxibatch_size:
            self._n_batch += (maxibatch_size - (self._n_batch % maxibatch_size))
        self.nb_examples = self._n_batch * batch_size

        #########################################################
        list_lang = [tgt_lang_insts[0]]
        list_start = [0]
        for ii, lang_token in enumerate(tgt_lang_insts):
            if lang_token == list_lang[-1]:
                continue
            else:
                list_start += [ii]
                list_lang += [tgt_lang_insts[ii]]

        sources = []
        targets = []
        tgt_langs = []
        src_langs = []
        if src_lang_insts is not None:
            src_lang_dict = {}
            src_val = 0
            new_src_lang_insts = []
            for elt in src_lang_insts:
                if elt[0] not in src_lang_dict:
                    src_lang_dict[elt[0]] = src_val
                    src_val += 1
                new_src_lang_insts.append([src_lang_dict[elt[0]]])
            src_lang_insts = new_src_lang_insts
        for ii, start_idx in enumerate(list_start):
            if ii < len(list_start)-1:
                sources += [src_insts[start_idx: list_start[ii+1]]]
                if tgt_insts:
                    targets += [tgt_insts[start_idx: list_start[ii+1]]]
                tgt_langs += [tgt_lang_insts[start_idx: list_start[ii+1]]]
                if src_lang_insts:
                    src_langs += [src_lang_insts[start_idx: list_start[ii+1]]]
            else:
                sources += [src_insts[start_idx:]]
                if tgt_insts:
                    targets += [tgt_insts[start_idx:]]
                tgt_langs += [tgt_lang_insts[start_idx:]]
                if src_lang_insts:
                    src_langs += [src_lang_insts[start_idx:]]
        ########################################################


        self._batch_size = batch_size

        #self._src_insts = src_insts
        #self._tgt_insts = tgt_insts
        #self._tgt_lang_insts = tgt_lang_insts
        self._sources = sources
        self._targets = targets if targets else None
        self._languages = tgt_langs
        self._src_langs = src_langs if src_langs else None

        src_idx2word = {idx:word for word, idx in src_word2idx.items()}
        tgt_idx2word = {idx:word for word, idx in tgt_word2idx.items()}

        self._src_word2idx = src_word2idx
        self._src_idx2word = src_idx2word

        self._tgt_word2idx = tgt_word2idx
        self._tgt_idx2word = tgt_idx2word

        self._iter_count = 0

        self._need_shuffle = shuffle

        self.is_train = is_train

        if self._need_shuffle:
            for ii in range(len(self._languages)):
                self.shuffle(ii)

        self._sort_by_length = sort_by_length

        self._maxibatch_size = maxibatch_size

        self._n_langs = len(self._languages)

        assert not ((self._maxibatch_size * self._batch_size) % self._n_langs)

        self._starts = [0] * self._n_langs

        for ii in range(self._n_langs):
            assert (self._batch_size * self._maxibatch_size)/self._n_langs <= len(self._sources[ii])

    @property
    def n_insts(self):
        ''' Property for dataset size '''
        return len(self._src_insts)

    @property
    def src_vocab_size(self):
        ''' Property for vocab size '''
        return len(self._src_word2idx)

    @property
    def tgt_vocab_size(self):
        ''' Property for vocab size '''
        return len(self._tgt_word2idx)

    @property
    def src_word2idx(self):
        ''' Property for word dictionary '''
        return self._src_word2idx

    @property
    def tgt_word2idx(self):
        ''' Property for word dictionary '''
        return self._tgt_word2idx

    @property
    def src_idx2word(self):
        ''' Property for index dictionary '''
        return self._src_idx2word

    @property
    def tgt_idx2word(self):
        ''' Property for index dictionary '''
        return self._tgt_idx2word

    def shuffle(self, idx):
        ''' Shuffle data for a brand new start '''
        if self._targets and self._src_langs:
            paired_insts = list(zip(self._sources[idx], self._targets[idx], self._src_langs[idx]))
            random.shuffle(paired_insts)
            self._sources[idx], self._targets[idx], self._src_langs[idx] = zip(*paired_insts)
        elif self._targets:
            paired_insts = list(zip(self._sources[idx], self._targets[idx]))
            random.shuffle(paired_insts)
            self._sources[idx], self._targets[idx] = zip(*paired_insts)
        elif self._src_langs:
            paired_insts = list(zip(self._sources[idx], self._src_langs[idx]))
            random.shuffle(paired_insts)
            self._sources[idx], self._src_langs[idx] = zip(*paired_insts)
        else:
            random.shuffle(self._sources[idx])

    def __iter__(self):
        return self

    def __next__(self):
        return self.next()

    def __len__(self):
        return self._n_batch

    def next(self):
        ''' Get the next batch '''

        def pad_to_longest(insts):
            ''' Pad the instance to the max seq length in batch '''

            max_len = max(len(inst) for inst in insts)
            
            inst_data = np.array([
                list(inst) + [Constants.PAD] * (max_len - len(inst))
                for inst in insts], dtype=np.int64)

            inst_position = np.array([
                [pos_i+1 if w_i != Constants.PAD else 0 for pos_i, w_i in enumerate(inst)]
                for inst in inst_data], dtype=np.int64)

            inst_data_tensor = Variable(torch.LongTensor(inst_data), volatile=(not self.is_train))
            inst_position_tensor = Variable(torch.LongTensor(inst_position), volatile=(not self.is_train))

            if self.cuda:
                inst_data_tensor = inst_data_tensor.cuda()
                inst_position_tensor = inst_position_tensor.cuda()
                
            return inst_data_tensor, inst_position_tensor

        if self._iter_count < self._n_batch:
            
            batch_idx = self._iter_count
            self._iter_count += 1


            if self._sort_by_length:

                #assert self._tgt_insts, 'Target must be provided to do sort_by_length'

                if batch_idx % self._maxibatch_size == 0:

                    src_insts = []
                    tgt_insts = []
                    tgt_lang_insts = []
                    src_lang_insts = []
                    
                    for ii, start_idx in enumerate(self._starts):
                        end_idx = start_idx + self._batch_size * self._maxibatch_size // self._n_langs
                        src_insts += self._sources[ii][start_idx:end_idx]
                        tgt_insts += self._targets[ii][start_idx:end_idx]
                        if self._languages:
                            tgt_lang_insts += list(self._languages[ii][start_idx:end_idx])
                        if self._src_langs:
                            src_lang_insts += list(self._src_langs[ii][start_idx:end_idx])
                        if end_idx < len(self._sources[ii]):
                            self._starts[ii] = end_idx
                        else:
                            delta = end_idx - len(self._sources[ii])
                            self.shuffle(ii)
                            src_insts += self._sources[ii][:delta]
                            tgt_insts += self._targets[ii][:delta]
                            if self._languages:
                                tgt_lang_insts += list(self._languages[ii][:delta])
                            if self._src_langs:
                                src_lang_insts += list(self._src_langs[ii][:delta])
                            self._starts[ii] = delta


                    slen = np.array([len(s) for s in src_insts])
                    sidx = slen.argsort()[::-1]

                    self._sbuf = [src_insts[i] for i in sidx]
                    self._tbuf = [tgt_insts[i] for i in sidx]
                    if self._languages:
                        self._cbuf = [tgt_lang_insts[i] for i in sidx]
                    if self._src_langs:
                        self._slbuf = [src_lang_insts[i] for i in sidx]
                    
                cur_start = (batch_idx % self._maxibatch_size) * self._batch_size
                cur_end = ((batch_idx % self._maxibatch_size) + 1) * self._batch_size

                cur_src_insts = self._sbuf[cur_start:cur_end]
                src_data, src_pos = pad_to_longest(cur_src_insts)

                cur_tgt_insts = self._tbuf[cur_start:cur_end]
                tgt_data, tgt_pos = pad_to_longest(cur_tgt_insts)

                if self._src_langs and self._languages:
                    cur_src_lang_insts = self._slbuf[cur_start:cur_end]
                    src_lang_data, src_lang_pos = pad_to_longest(cur_src_lang_insts)

                    cur_tgt_lang_insts = self._cbuf[cur_start:cur_end]
                    tgt_lang_data, tgt_lang_pos = pad_to_longest(cur_tgt_lang_insts)

                    return (src_data, src_pos), (tgt_data, tgt_pos), (src_lang_data, src_lang_pos), (tgt_lang_data, tgt_lang_pos)
                elif self._src_langs:
                    cur_src_lang_insts = self._slbuf[cur_start:cur_end]
                    src_lang_data, src_lang_pos = pad_to_longest(cur_src_lang_insts)

                    return (src_data, src_pos), (tgt_data, tgt_pos), (src_lang_data, src_lang_pos)
                elif self._languages:
                    cur_tgt_lang_insts = self._cbuf[cur_start:cur_end]
                    tgt_lang_data, tgt_lang_pos = pad_to_longest(cur_tgt_lang_insts)

                    return (src_data, src_pos), (tgt_data, tgt_pos), (tgt_lang_data, tgt_lang_pos)
                else:
                    return (src_data, src_pos), (tgt_data, tgt_pos)

            else:
                raise NotImplementedError

        else:
            self._iter_count = 0
            raise StopIteration()
