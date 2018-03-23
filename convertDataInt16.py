import numpy as np
import argparse
import pickle

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-data', required=True)
    opt = parser.parse_args()

    #import ipdb; ipdb.set_trace()
    
    data = torch.load(opt.data)

    for set_ in ['train', 'valid']:
        for key in ['src', 'tgt', 'src_lang', 'tgt_lang']:
            if key == 'src' or key == 'tgt':
                data[set_][key] = [np.asarray(elt, dtype=np.uint16) for elt in data[set_][key]]
            else:
                data[set_][key] = np.asarray(data[set_][key], dtype=np.uint16)
                
            #import ipdb; ipdb.set_trace()
            

    #torch.save(data, opt.data.split('.')[0]+'_16.pt')
    with open(opt.data.split('.')[0]+'_16.pkl', 'wb') as f:
        pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)

if __name__ == '__main__':
    main()
