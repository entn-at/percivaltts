'''
The base model class to derive the others from.

Copyright(C) 2017 Engineering Department, University of Cambridge, UK.

License
   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at
     http://www.apache.org/licenses/LICENSE-2.0
   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.

Author
    Gilles Degottex <gad27@cam.ac.uk>
'''

from __future__ import print_function

from percivaltts import *  # Always include this first to setup a few things

import sys
import os
import copy
import time

import cPickle
from collections import defaultdict

import numpy as np
numpy_force_random_seed()

from backend_theano import *
import theano
import theano.tensor as T

import lasagne
# lasagne.random.set_rng(np.random)

from external.pulsemodel import sigproc as sp

import data

if th_cuda_available():
    from pygpu.gpuarray import GpuArrayException   # pragma: no cover
else:
    class GpuArrayException(Exception): pass       # declare a dummy one if pygpu is not loaded


class Optimizer:

    # A few hardcoded values
    _LSWGANtransfreqcutoff = 4000 # [Hz] Params hardcoded
    _LSWGANtranscoef = 1.0/8.0 # Params hardcoded
    _WGAN_incnoisefeature = False # Set it to True to include noise in the WGAN loss

    # Variables
    _errtype = 'WGAN' # or 'LSE'
    _model = None # The model whose parameters will be optimised.
    _target_values = None
    _params_trainable = None
    _optim_updates = []  # The variables of the optimisation algo, for restoring a training that crashed

    def __init__(self, model, errtype='WGAN'):
        self._model = model

        self._errtype = errtype

        self._target_values = T.ftensor3('target_values')

    def saveTrainingState(self, fstate, cfg=None, extras=None, printfn=print):
        # https://github.com/Lasagne/Lasagne/issues/159
        if extras is None: extras=dict()
        printfn('    saving training state in {} ...'.format(fstate), end='')
        sys.stdout.flush()

        paramsvalues = [(str(p), p.get_value()) for p in self._model.params_all] # The network parameters

        ovs = []
        for ov in self._optim_updates:
            ovs.append([p.get_value() for p in ov.keys()]) # The optim algo state

        DATA = [paramsvalues, ovs, cfg, extras, np.random.get_state()]
        cPickle.dump(DATA, open(fstate, 'wb'))

        print(' done')
        sys.stdout.flush()

    def loadTrainingState(self, fstate, cfg, printfn=print):
        # https://github.com/Lasagne/Lasagne/issues/159
        printfn('    reloading parameters from {} ...'.format(fstate), end='')
        sys.stdout.flush()

        DATA = cPickle.load(open(fstate, 'rb'))
        for p, v in zip(self._model.params_all, DATA[0]): p.set_value(v[1])    # The network parameters

        for ov, da in zip(self._optim_updates, DATA[1]):
            for p, value in zip(ov.keys(), da): p.set_value(value)

        print(' done')
        sys.stdout.flush()

        if cfg.__dict__!=DATA[2].__dict__:
            printfn('        configurations are not the same!')
            for attr in cfg.__dict__:
                if attr in DATA[2].__dict__:
                    if cfg.__dict__[attr]!=DATA[2].__dict__[attr]:
                        print('            attribute {}: new state {}, saved state {}'.format(attr, cfg.__dict__[attr], DATA[2].__dict__[attr]))
                else:
                    print('            attribute {}: is not in the saved configuration state'.format(attr))
            for attr in DATA[2].__dict__:
                if attr not in cfg.__dict__:
                    print('            attribute {}: is not in the new configuration state'.format(attr))

        return DATA[2:]

    # Training =================================================================

    def train(self, params, indir, outdir, wdir, fid_lst_tra, fid_lst_val, X_vals, Y_vals, cfg, params_savefile, trialstr='', cont=None):

        print('Model initial status before training')
        worst_val = data.cost_0pred_rmse(Y_vals) # RMSE
        print("    0-pred validation RMSE = {} (100%)".format(worst_val))
        init_pred_rms = data.prediction_rms(self._model, [X_vals])
        print('    initial RMS of prediction = {}'.format(init_pred_rms))
        init_val = data.cost_model_prediction_rmse(self._model, [X_vals], Y_vals)
        best_val = None
        print("    initial validation RMSE = {} ({:.4f}%)".format(init_val, 100.0*init_val/worst_val))

        nbbatches = int(len(fid_lst_tra)/cfg.train_batch_size)
        print('    using {} batches of {} sentences each'.format(nbbatches, cfg.train_batch_size))
        print('    model #parameters={}'.format(self._model.nbParams()))

        nbtrainframes = 0
        for fid in fid_lst_tra:
            X = data.loadfile(outdir, fid)
            nbtrainframes += X.shape[0]
        frameshift = 0.005 # TODO
        print('    Training set: {} sentences, #frames={} ({})'.format(len(fid_lst_tra), nbtrainframes, time.strftime('%H:%M:%S', time.gmtime((nbtrainframes*frameshift)))))
        print('    #parameters/#frames={:.2f}'.format(float(self._model.nbParams())/nbtrainframes))
        if cfg.train_nbepochs_scalewdata and not cfg.train_batch_lengthmax is None:
            # During an epoch, the whole data is _not_ seen by the training since cfg.train_batch_lengthmax is limited and smaller to the sentence size.
            # To compensate for it and make the config below less depedent on the data, the min ans max nbepochs are scaled according to the missing number of frames seen.
            # TODO Should consider only non-silent frames, many recordings have a lot of pre and post silences
            epochcoef = nbtrainframes/float((cfg.train_batch_lengthmax*len(fid_lst_tra)))
            print('    scale number of epochs wrt number of frames')
            cfg.train_min_nbepochs = int(cfg.train_min_nbepochs*epochcoef)
            cfg.train_max_nbepochs = int(cfg.train_max_nbepochs*epochcoef)
            print('        train_min_nbepochs={}'.format(cfg.train_min_nbepochs))
            print('        train_max_nbepochs={}'.format(cfg.train_max_nbepochs))

        if self._errtype=='WGAN':
            print('Preparing critic for WGAN...')
            critic_input_var = T.tensor3('critic_input') # Either real data to predict/generate, or, fake data that has been generated

            [critic, layer_critic, layer_cond] = self._model.build_critic(critic_input_var, self._model._input_values, self._model.vocoder, self._model.insize, use_LSweighting=(cfg.train_LScoef>0.0), LSWGANtransfreqcutoff=self._LSWGANtransfreqcutoff, LSWGANtranscoef=self._LSWGANtranscoef, use_WGAN_incnoisefeature=self._WGAN_incnoisefeature)

            # Create expression for passing real data through the critic
            real_out = lasagne.layers.get_output(critic)
            # Create expression for passing fake data through the critic
            genout = lasagne.layers.get_output(self._model.net_out)
            indict = {layer_critic:lasagne.layers.get_output(self._model.net_out), layer_cond:self._model._input_values}
            fake_out = lasagne.layers.get_output(critic, indict)

            # Create generator's loss expression
            # Force LSE for low frequencies, otherwise the WGAN noise makes the voice hoarse.
            print('WGAN Weighted LS - Generator part')

            wganls_weights_els = []
            wganls_weights_els.append([0.0]) # For f0
            specvs = np.arange(self._model.vocoder.specsize(), dtype=theano.config.floatX)
            if cfg.train_LScoef==0.0:
                wganls_weights_els.append(np.ones(self._model.vocoder.specsize()))  # No special weighting for spec
            else:
                wganls_weights_els.append(nonlin_sigmoidparm(specvs,  sp.freq2fwspecidx(self._LSWGANtransfreqcutoff, self._model.vocoder.fs, self._model.vocoder.specsize()), self._LSWGANtranscoef)) # For spec
            if self._model.vocoder.noisesize()>0:
                if self._WGAN_incnoisefeature:
                    noisevs = np.arange(self._model.vocoder.noisesize(), dtype=theano.config.floatX)
                    wganls_weights_els.append(nonlin_sigmoidparm(noisevs,  sp.freq2fwspecidx(self._LSWGANtransfreqcutoff, self._model.vocoder.fs, self._model.vocoder.noisesize()), self._LSWGANtranscoef)) # For noise
                else:
                    wganls_weights_els.append(np.zeros(self._model.vocoder.noisesize()))
            if self._model.vocoder.vuvsize()>0:
                wganls_weights_els.append([0.0]) # For vuv
            wganls_weights_ = np.hstack(wganls_weights_els)

            # TODO build wganls_weights_ for LSE instead for WGAN, for consistency with the paper

            # wganls_weights_ = np.hstack((wganls_weights_, wganls_weights_, wganls_weights_)) # That would be for MLPG using deltas
            wganls_weights_ *= (1.0-cfg.train_LScoef)

            lserr = lasagne.objectives.squared_error(genout, self._target_values)
            wganls_weights_ls = theano.shared(value=(1.0-wganls_weights_), name='wganls_weights_ls')

            wganpart = fake_out*np.mean(wganls_weights_)  # That's a way to automatically balance the WGAN and LSE costs wrt the LSE spectral weighting
            lsepart = lserr*wganls_weights_ls             # Spectral weighting as complement of the WGAN part spectral weighting

            generator_loss = -wganpart.mean() + lsepart.mean() # A term in [-oo,oo] and one in [0,oo] ... why not, LSE as to be small enough for WGAN to do something.

            generator_lossratio = abs(wganpart.mean())/abs(lsepart.mean())

            critic_loss = fake_out.mean() - real_out.mean()  # For clarity: we want to maximum real-fake -> -(real-fake) -> fake-real

            # Improved training for Wasserstein GAN
            epsi = T.TensorType(dtype=theano.config.floatX,broadcastable=(False, True, True))()
            mixed_X = (epsi * genout) + (1-epsi) * critic_input_var
            indict = {layer_critic:mixed_X, layer_cond:self._model._input_values}
            output_D_mixed = lasagne.layers.get_output(critic, inputs=indict)
            grad_mixed = T.grad(T.sum(output_D_mixed), mixed_X)
            norm_grad_mixed = T.sqrt(T.sum(T.square(grad_mixed),axis=[1,2]))
            grad_penalty = T.mean(T.square(norm_grad_mixed -1))
            critic_loss = critic_loss + cfg.train_pg_lambda*grad_penalty

            # Create update expressions for training
            critic_params = lasagne.layers.get_all_params(critic, trainable=True)
            critic_updates = lasagne.updates.adam(critic_loss, critic_params, learning_rate=cfg.train_D_learningrate, beta1=cfg.train_D_adam_beta1, beta2=cfg.train_D_adam_beta2)
            print('    Critic architecture')
            print_network(critic, critic_params)

            generator_params = lasagne.layers.get_all_params(self._model.net_out, trainable=True)
            generator_updates = lasagne.updates.adam(generator_loss, generator_params, learning_rate=cfg.train_G_learningrate, beta1=cfg.train_G_adam_beta1, beta2=cfg.train_G_adam_beta2)
            self._optim_updates.extend([generator_updates, critic_updates])
            print('    Generator architecture')
            print_network(self._model.net_out, generator_params)

            # Compile functions performing a training step on a mini-batch (according
            # to the updates dictionary) and returning the corresponding score:
            print('Compiling generator training function...')
            generator_train_fn_ins = [self._model._input_values]
            generator_train_fn_ins.append(self._target_values)
            generator_train_fn_outs = [generator_loss, generator_lossratio]
            train_fn = theano.function(generator_train_fn_ins, generator_train_fn_outs, updates=generator_updates)
            train_validation_fn = theano.function(generator_train_fn_ins, generator_loss, no_default_updates=True)
            print('Compiling critic training function...')
            critic_train_fn_ins = [self._model._input_values, critic_input_var, epsi]
            critic_train_fn = theano.function(critic_train_fn_ins, critic_loss, updates=critic_updates)
            critic_train_validation_fn = theano.function(critic_train_fn_ins, critic_loss, no_default_updates=True)

        elif self._errtype=='LSE':
            print('    LSE Training')
            print_network(self._model.net_out, params)
            predicttrain_values = lasagne.layers.get_output(self._model.net_out, deterministic=False)
            costout = (predicttrain_values - self._target_values)**2

            self.cost = T.mean(costout) # self.cost = T.mean(T.sum(costout, axis=-1)) ?

            print("    creating parameters updates ...")
            updates = lasagne.updates.adam(self.cost, params, learning_rate=float(10**cfg.train_learningrate_log10), beta1=float(cfg.train_adam_beta1), beta2=float(cfg.train_adam_beta2), epsilon=float(10**cfg.train_adam_epsilon_log10))

            self._optim_updates.append(updates)
            print("    compiling training function ...")
            train_fn = theano.function(self._model.inputs+[self._target_values], self.cost, updates=updates)
        else:
            raise ValueError('Unknown err type "'+self._errtype+'"')    # pragma: no cover

        costs = defaultdict(list)
        epochs_modelssaved = []
        epochs_durs = []
        nbnodecepochs = 0
        generator_updates = 0
        epochstart = 1
        if cont and os.path.exists(os.path.splitext(params_savefile)[0]+'-trainingstate-last.pkl'):
            print('    reloading previous training state ...')
            savedcfg, extras, rngstate = self.loadTrainingState(os.path.splitext(params_savefile)[0]+'-trainingstate-last.pkl', cfg)
            np.random.set_state(rngstate)
            cost_val = extras['cost_val']
            # Restoring some local variables
            costs = extras['costs']
            epochs_modelssaved = extras['epochs_modelssaved']
            epochs_durs = extras['epochs_durs']
            generator_updates = extras['generator_updates']
            epochstart = extras['epoch']+1
            # Restore the saving criteria only none of those 3 cfg values changed:
            if (savedcfg.train_min_nbepochs==cfg.train_min_nbepochs) and (savedcfg.train_max_nbepochs==cfg.train_max_nbepochs) and (savedcfg.train_cancel_nodecepochs==cfg.train_cancel_nodecepochs):
                best_val = extras['best_val']
                nbnodecepochs = extras['nbnodecepochs']

        print_log("    start training ...")
        for epoch in range(epochstart,1+cfg.train_max_nbepochs):
            timeepochstart = time.time()
            rndidx = np.arange(int(nbbatches*cfg.train_batch_size))    # Need to restart from ordered state to make the shuffling repeatable after reloading training state, the shuffling will be different anyway
            np.random.shuffle(rndidx)
            rndidxb = np.split(rndidx, nbbatches)
            cost_tra = None
            costs_tra_batches = []
            costs_tra_gen_wgan_lse_ratios = []
            costs_tra_critic_batches = []
            load_times = []
            train_times = []
            for k in xrange(nbbatches):

                timeloadstart = time.time()
                print_tty('\r    Training batch {}/{}'.format(1+k, nbbatches))

                # Load training data online, because data is often too heavy to hold in memory
                fid_lst_trab = [fid_lst_tra[bidx] for bidx in rndidxb[k]]
                X_trab, _, Y_trab, _, W_trab = data.load_inoutset(indir, outdir, wdir, fid_lst_trab, length=cfg.train_batch_length, lengthmax=cfg.train_batch_lengthmax, maskpadtype=cfg.train_batch_padtype, cropmode=cfg.train_batch_cropmode)

                if 0: # Plot batch
                    import matplotlib.pyplot as plt
                    plt.ion()
                    plt.imshow(Y_trab[0,].T, origin='lower', aspect='auto', interpolation='none', cmap='jet')
                    from IPython.core.debugger import  Pdb; Pdb().set_trace()

                load_times.append(time.time()-timeloadstart)
                print_tty(' (iter load: {:.6f}s); training '.format(load_times[-1]))

                timetrainstart = time.time()
                if self._errtype=='WGAN':

                    random_epsilon = np.random.uniform(size=(cfg.train_batch_size, 1,1)).astype('float32')
                    critic_returns = critic_train_fn(X_trab, Y_trab, random_epsilon)        # Train the criticmnator
                    costs_tra_critic_batches.append(float(critic_returns))

                    # TODO The params below are supposed to ensure the critic is "almost" fully converged
                    #      when training the generator. How to evaluate this? Is it the case currently?
                    if (generator_updates < 25) or (generator_updates % 500 == 0):  # TODO Params hardcoded
                        critic_runs = 10 # TODO Params hardcoded 10
                    else:
                        critic_runs = 5 # TODO Params hardcoded 5
                    # martinarjovsky: "- Loss of the critic should never be negative, since outputing 0 would yeald a better loss so this is a huge red flag."
                    # if critic_returns>0 and k%critic_runs==0: # Train only if the estimate of the Wasserstein distance makes sense, and, each N critic iteration TODO Doesn't work well though
                    if k%critic_runs==0: # Train each N critic iteration
                        # Train the generator
                        trainargs = [X_trab]
                        trainargs.append(Y_trab)
                        [cost_tra, gen_ratio] = train_fn(*trainargs)
                        cost_tra = float(cost_tra)
                        generator_updates += 1

                        if 0: log_plot_samples(Y_vals, Y_preds, nbsamples=nbsamples, fname=os.path.splitext(params_savefile)[0]+'-fig_samples_'+trialstr+'{:07}.png'.format(generator_updates), vocoder=self._model.vocoder, title='E{} I{}'.format(epoch,generator_updates))

                elif self._errtype=='LSE':
                    train_returns = train_fn(X_trab, Y_trab)
                    cost_tra = np.sqrt(float(train_returns))

                train_times.append(time.time()-timetrainstart)

                if not cost_tra is None:
                    print_tty('err={:.4f} (iter train: {:.4f}s)                  '.format(cost_tra,train_times[-1]))
                    if np.isnan(cost_tra):                      # pragma: no cover
                        print_log('    previous costs: {}'.format(costs_tra_batches))
                        print_log('    E{} Batch {}/{} train cost = {}'.format(epoch, 1+k, nbbatches, cost_tra))
                        raise ValueError('ERROR: Training cost is nan!')
                    costs_tra_batches.append(cost_tra)
                    if self._errtype=='WGAN': costs_tra_gen_wgan_lse_ratios.append(gen_ratio)
            print_tty('\r                                                           \r')
            if self._errtype=='WGAN':
                costs['model_training'].append(0.1*np.mean(costs_tra_batches))
                if cfg.train_LScoef>0: costs['model_training_wgan_lse_ratio'].append(0.1*np.mean(costs_tra_gen_wgan_lse_ratios))
            else:
                costs['model_training'].append(np.mean(costs_tra_batches))

            # Eval validation cost
            cost_validation_rmse = data.cost_model_prediction_rmse(self._model, [X_vals], Y_vals)
            costs['model_rmse_validation'].append(cost_validation_rmse)

            if self._errtype=='WGAN':
                train_validation_fn_args = [X_vals]
                train_validation_fn_args.append(Y_vals)
                costs['model_validation'].append(0.1*data.cost_model_mfn(train_validation_fn, train_validation_fn_args))
                costs['critic_training'].append(np.mean(costs_tra_critic_batches))
                random_epsilon = [np.random.uniform(size=(1,1)).astype('float32')]*len(X_vals)
                critic_train_validation_fn_args = [X_vals, Y_vals, random_epsilon]
                costs['critic_validation'].append(data.cost_model_mfn(critic_train_validation_fn, critic_train_validation_fn_args))
                costs['critic_validation_ltm'].append(np.mean(costs['critic_validation'][-cfg.train_validation_ltm_winlen:]))

                cost_val = costs['critic_validation_ltm'][-1]
            elif self._errtype=='LSE':
                cost_val = costs['model_rmse_validation'][-1]

            print_log("    E{}/{} {}  cost_tra={:.6f} (load:{}s train:{}s)  cost_val={:.6f} ({:.4f}% RMSE)  {} MiB GPU {} MiB RAM".format(epoch, cfg.train_max_nbepochs, trialstr, costs['model_training'][-1], time2str(np.sum(load_times)), time2str(np.sum(train_times)), cost_val, 100*cost_validation_rmse/worst_val, nvidia_smi_gpu_memused(), proc_memresident()))
            sys.stdout.flush()

            if np.isnan(cost_val): raise ValueError('ERROR: Validation cost is nan!')
            if (self._errtype=='LSE') and (cost_val>=cfg.train_cancel_validthresh*worst_val): raise ValueError('ERROR: Validation cost blew up! It is higher than {} times the worst possible values'.format(cfg.train_cancel_validthresh))

            self._model.saveAllParams(os.path.splitext(params_savefile)[0]+'-last.pkl', cfg=cfg, printfn=print_log, extras={'cost_val':cost_val})

            # Save model parameters
            if epoch>=cfg.train_min_nbepochs: # Assume no model is good enough before cfg.train_min_nbepochs
                if ((best_val is None) or (cost_val<best_val)): # Among all trials of hyper-parameter optimisation
                    best_val = cost_val
                    self._model.saveAllParams(params_savefile, cfg=cfg, printfn=print_log, extras={'cost_val':cost_val}, infostr='(E{} C{:.4f})'.format(epoch, best_val))
                    epochs_modelssaved.append(epoch)
                    nbnodecepochs = 0
                else:
                    nbnodecepochs += 1

            if cfg.train_log_plot:
                print_log('    saving plots')
                log_plot_costs(costs, worst_val, fname=os.path.splitext(params_savefile)[0]+'-fig_costs_'+trialstr+'.svg', epochs_modelssaved=epochs_modelssaved)

                nbsamples = 2
                nbsamples = min(nbsamples, len(X_vals))
                Y_preds = []
                for sampli in xrange(nbsamples): Y_preds.append(self._model.predict(np.reshape(X_vals[sampli],[1]+[s for s in X_vals[sampli].shape]))[0,])

                plotsuffix = ''
                if len(epochs_modelssaved)>0 and epochs_modelssaved[-1]==epoch: plotsuffix='_best'
                else:                                                           plotsuffix='_last'
                log_plot_samples(Y_vals, Y_preds, nbsamples=nbsamples, fname=os.path.splitext(params_savefile)[0]+'-fig_samples_'+trialstr+plotsuffix+'.png', vocoder=self._model.vocoder, title='E{}'.format(epoch))

            epochs_durs.append(time.time()-timeepochstart)
            print_log('    ET: {}   max TT: {}s   train ~time left: {}'.format(time2str(epochs_durs[-1]), time2str(np.median(epochs_durs[-10:])*cfg.train_max_nbepochs), time2str(np.median(epochs_durs[-10:])*(cfg.train_max_nbepochs-epoch))))

            self.saveTrainingState(os.path.splitext(params_savefile)[0]+'-trainingstate-last.pkl', cfg=cfg, printfn=print_log, extras={'cost_val':cost_val, 'best_val':best_val, 'costs':costs, 'epochs_modelssaved':epochs_modelssaved, 'epochs_durs':epochs_durs, 'nbnodecepochs':nbnodecepochs, 'generator_updates':generator_updates, 'epoch':epoch})

            if nbnodecepochs>=cfg.train_cancel_nodecepochs: # pragma: no cover
                print_log('WARNING: validation error did not decrease for {} epochs. Early stop!'.format(cfg.train_cancel_nodecepochs))
                break

        if best_val is None: raise ValueError('No model has been saved during training!')
        return {'epoch_stopped':epoch, 'worst_val':worst_val, 'best_epoch':epochs_modelssaved[-1] if len(epochs_modelssaved)>0 else -1, 'best_val':best_val}


    @classmethod
    def randomize_hyper(cls, cfg):
        cfg = copy.copy(cfg) # Create a new one instead of updating the object passed as argument

        # Randomized the hyper parameters
        if len(cfg.train_hypers)<1: return cfg, ''

        hyperstr = ''
        for hyper in cfg.train_hypers:
            if isinstance(hyper[1], int) and isinstance(hyper[2], int):
                setattr(cfg, hyper[0], np.random.randint(hyper[1],hyper[2]))
            else:
                setattr(cfg, hyper[0], np.random.uniform(hyper[1],hyper[2]))
            hyperstr += hyper[0]+'='+str(getattr(cfg, hyper[0]))+','
        hyperstr = hyperstr[:-1]

        return cfg, hyperstr

    def train_multipletrials(self, indir, outdir, wdir, fid_lst_tra, fid_lst_val, params, params_savefile, cfgtomerge=None, cont=None, **kwargs):
        # Hyp: always uses batches

        # All kwargs arguments are specific configuration values
        # First, fill a struct with the default configuration values ...
        cfg = configuration() # Init structure

        # LSE
        cfg.train_learningrate_log10 = -3.39794 # [potential hyper-parameter] (10**-3.39794=0.0004)
        cfg.train_adam_beta1 = 0.9              # [potential hyper-parameter]
        cfg.train_adam_beta2 = 0.999            # [potential hyper-parameter]
        cfg.train_adam_epsilon_log10 = -8       # [potential hyper-parameter]
        # WGAN
        cfg.train_D_learningrate = 0.0001       # [potential hyper-parameter]
        cfg.train_D_adam_beta1 = 0.5            # [potential hyper-parameter]
        cfg.train_D_adam_beta2 = 0.9            # [potential hyper-parameter]
        cfg.train_G_learningrate = 0.001        # [potential hyper-parameter]
        cfg.train_G_adam_beta1 = 0.5            # [potential hyper-parameter]
        cfg.train_G_adam_beta2 = 0.9            # [potential hyper-parameter]
        cfg.train_pg_lambda = 10                # [potential hyper-parameter]
        cfg.train_LScoef = 0.25                 # If >0, mix LSE and WGAN losses (def. 0.25)
        cfg.train_validation_ltm_winlen = 20    # Now that I'm using min and max epochs, I could use the actuall D cost and not the ltm(D cost) TODO

        cfg.train_min_nbepochs = 200
        cfg.train_max_nbepochs = 300
        cfg.train_nbepochs_scalewdata = True
        cfg.train_cancel_nodecepochs = 50
        cfg.train_cancel_validthresh = 10.0     # Cancel train if valid err is more than N times higher than the initial worst valid err
        cfg.train_batch_size = 5                # [potential hyper-parameter]
        cfg.train_batch_padtype = 'randshift'   # See load_inoutset(..., maskpadtype)
        cfg.train_batch_cropmode = 'begendbigger'     # 'begend', 'begendbigger', 'all'
        cfg.train_batch_length = None           # Duration [frames] of each batch (def. None, i.e. the shortest duration of the batch if using maskpadtype = 'randshift') # TODO Remove for lengthmax
        cfg.train_batch_lengthmax = None        # Maximum duration [frames] of each batch
        cfg.train_nbtrials = 1                  # Just run one training only
        cfg.train_hypers=[]
        #cfg.train_hypers = [('learningrate_log10', -6.0, -2.0), ('adam_beta1', 0.8, 1.0)] # For ADAM
        #cfg.train_hyper = [('train_D_learningrate', 0.0001, 0.1), ('train_D_adam_beta1', 0.8, 1.0), ('train_D_adam_beta2', 0.995, 1.0), ('train_batch_size', 1, 200)] # For ADAM
        cfg.train_log_plot=True
        # ... add/overwrite configuration from cfgtomerge ...
        if not cfgtomerge is None: cfg.merge(cfgtomerge)
        # ... and add/overwrite specific configuration from the generic arguments
        for kwarg in kwargs.keys(): setattr(cfg, kwarg, kwargs[kwarg])

        print('Training configuration')
        cfg.print_content()

        print('Loading all validation data at once ...')
        # X_val, Y_val = data.load_inoutset(indir, outdir, wdir, fid_lst_val, verbose=1)
        X_vals = data.load(indir, fid_lst_val, verbose=1, label='Context labels: ')
        Y_vals = data.load(outdir, fid_lst_val, verbose=1, label='Output features: ')
        X_vals, Y_vals = data.croplen([X_vals, Y_vals])
        print('    {} validation files'.format(len(fid_lst_val)))
        print('    {:.2f}% of validation files for number of train files'.format(100.0*float(len(fid_lst_val))/len(fid_lst_tra)))

        if cfg.train_nbtrials>1:
            self._model.saveAllParams(os.path.splitext(params_savefile)[0]+'-init.pkl', cfg=cfg, printfn=print_log)

        try:
            trials = []
            for triali in xrange(1,1+cfg.train_nbtrials):  # Run multiple trials with different hyper-parameters
                print('\nStart trial {} ...'.format(triali))

                try:
                    train_rets = None
                    trialstr = 'trial'+str(triali)
                    if len(cfg.train_hypers)>0:
                        cfg, hyperstr = self.randomize_hyper(cfg)
                        trialstr += ','+hyperstr
                        print('    randomized hyper-parameters: '+trialstr)
                    if cfg.train_nbtrials>1:
                        self._model.loadAllParams(os.path.splitext(params_savefile)[0]+'-init.pkl')

                    timewholetrainstart = time.time()
                    train_rets = self.train(params, indir, outdir, wdir, fid_lst_tra, fid_lst_val, X_vals, Y_vals, cfg, params_savefile, trialstr=trialstr, cont=cont)
                    cont = None
                    print_log('Total trial run time: {}s'.format(time2str(time.time()-timewholetrainstart)))

                except KeyboardInterrupt:                   # pragma: no cover
                    raise KeyboardInterrupt
                except (ValueError, GpuArrayException):     # pragma: no cover
                    if len(cfg.train_hypers)>0:
                        print_log('WARNING: Training crashed!')
                        import traceback
                        traceback.print_exc()
                    else:
                        print_log('ERROR: Training crashed!')
                        raise   # Crash the whole training if there is only one trial

                if cfg.train_nbtrials>1:
                    # Save the results of each trial, but only the non-crashed trials
                    if not train_rets is None:
                        ntrialline = [triali]+[getattr(cfg, field[0]) for field in cfg.train_hypers]
                        ntrialline = ntrialline+[train_rets[key] for key in sorted(train_rets.keys())]
                        header='trials '+' '.join([field[0] for field in cfg.train_hypers])+' '+' '.join(sorted(train_rets.keys()))
                        trials.append(ntrialline)
                        np.savetxt(os.path.splitext(params_savefile)[0]+'-trials.txt', np.vstack(trials), header=header)

        except KeyboardInterrupt:                           # pragma: no cover
            print_log('WARNING: Training interrupted by user!')

        print_log('Finished')
