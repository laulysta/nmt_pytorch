'''A wrapper class for optimizer '''
import numpy as np
from collections import OrderedDict

class ScheduledOptim(object):
    '''A simple wrapper class for learning rate scheduling'''

    def __init__(self, optimizer, d_model, n_warmup_steps):
        self.optimizer = optimizer
        self.d_model = d_model
        self.n_warmup_steps = n_warmup_steps
        self.n_current_steps = 0

    def state_dict(self):
        dict_ = OrderedDict()

        dict_['optimizer'] = self.optimizer.state_dict()

        dict_['d_model'] = self.d_model
        dict_['n_warmup_steps'] = self.n_warmup_steps
        dict_['n_current_steps'] = self.n_current_steps

        return dict_

    def load_state_dict(self, dict_):
        self.optimizer.load_state_dict(dict_['optimizer'])

        self.d_model = dict_['d_model']
        self.n_warmup_steps = dict_['n_warmup_steps']
        self.n_current_steps = dict_['n_current_steps']

    def step(self):
        "Step by the inner optimizer"
        self.optimizer.step()

    def zero_grad(self):
        "Zero out the gradients by the inner optimizer"
        self.optimizer.zero_grad()

    def update_learning_rate(self):
        ''' Learning rate scheduling per step '''

        self.n_current_steps += 1
        new_lr = np.power(self.d_model, -0.5) * np.min([
            np.power(self.n_current_steps, -0.5),
            np.power(self.n_warmup_steps, -1.5) * self.n_current_steps])

        for param_group in self.optimizer.param_groups:
            param_group['lr'] = new_lr
