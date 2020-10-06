import emcee
import time
import torch
import torch.nn as nn
from tqdm import tqdm
import numpy as np
import sklearn
import os
from ..utils import is_notebook
from pyro.infer import MCMC, NUTS, Predictive


class Sequential():
    def __init__(self, priors, obs_data, model, optimizer,
                 simulator=None, param_names=None,
                 num_initial_samples=250, num_samples_per_round=250,
                 scaler=None, obs_truth=None, n_rounds=10, sims_per_model=1,
                 mcmc_walkers=5, mcmc_steps=250, mcmc_discard=50, mcmc_thin=1,
                 max_n_epochs=200, valid_fraction=0.15, batch_size=50, grad_clip=5.,
                 patience=10,
                 log_dir='./', device=None):
        """
        Parameters
            simulator: callable
                A wrapper for the simulator, should consume (*, param_dim)
                arrays, possess a `sims_per_model` argument, and return
                a (*, sims_per_model, data_dim) array
            priors: dict
                Dictionary of priors {name: lbi.inference.priors.Prior}
            obs_data: np.ndarray (*, data_dim)
                Batch of observed data
            model: lbi.models.ConditionalFlow
                Model which will act as the approximate likelihood
            optimizer: torch.optim.Optimizer
                Optimizer for model
            scaler: sklearn.preprocessing.StandardScaler
                Scaler for data if required
            obs_truth: np.ndarray (*, param_dim)
                Batch of observed true params matching obs_data
            n_rounds: int
                Number of SNL rounds
            sims_per_model: int
                Number of simulations to generate per MCMC sample
            mcmc_walkers: int
                Number of independent MCMC walkers
            mcmc_steps: int
                Number of MCMC steps per round per walker
            mcmc_discard: int
                Number of MCMC steps per round per walker to discard
            mcmc_thin: int
                Take every `mcmc_thin` samples from MCMC chain
            max_n_epochs: int
                Number of epochs to train per SNL round
            valid_fraction: float
                Fraction of simulations to hold out for validation
            batch_size: int
                Number of training samples to estimate gradient from
            grad_clip: float
                Value at which to clip the gradient norm during training
            log_dir: str
                Location to store models and logs
            device: torch.device
                Device to train model on
        """

        self.priors = priors
        self.obs_data = obs_data
        self.model = model
        self.optimizer = optimizer
        self.simulator = simulator
        self.param_dim = priors.mean.shape[0]
        self.param_names = param_names
        self.data_dim = obs_data.shape[1]
        self.scaler = scaler
        self.obs_truth = obs_truth
        self.n_rounds = n_rounds
        self.num_initial_samples = num_initial_samples
        self.num_samples_per_round = num_samples_per_round
        self.sims_per_model = sims_per_model
        self.mcmc_steps = mcmc_steps
        self.mcmc_discard = mcmc_discard
        self.mcmc_thin = mcmc_thin
        self.mcmc_walkers = mcmc_walkers
        self.max_n_epochs = max_n_epochs
        self.patience = patience
        self.valid_fraction = valid_fraction
        self.batch_size = batch_size
        self.grad_clip = grad_clip
        self.log_dir = log_dir
        self.model_path = os.path.join(log_dir, 'lbi.pt')
        self.best_val_loss = np.inf
        self.notebook = is_notebook()


        if device is not None:
            self.device = device
        else:
            self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

        self.data = {
            'train_data': torch.empty([0, self.data_dim]).to(self.device),
            'train_params': torch.empty([0, self.param_dim]).to(self.device),
            'valid_data': torch.empty([0, self.data_dim]).to(self.device),
            'valid_params': torch.empty([0, self.param_dim]).to(self.device)}
        
        if self.scaler is not None:
            obs_data = obs_data.cpu().numpy()
            obs_data = self.scaler.transform(obs_data)
            obs_data = torch.from_numpy(obs_data).float().to(self.device)
        self.x0 = obs_data

    def add_data(self, data, params):
        if self.scaler is not None:
            data = data.cpu().numpy()
            data = self.scaler.transform(data)
            data = torch.from_numpy(data).float().to(self.device)

        data.to(self.device)

        # Select samples for validation
        n = data.shape[0]
        idx = sklearn.utils.shuffle(np.arange(n))
        m = int(self.valid_fraction * n)
        valid_idx = idx[:m]
        train_idx = idx[m:]

        # Store samples in dictionary
        self.data['train_data'] = torch.cat([self.data['train_data'], data[train_idx]], dim=0)
        self.data['train_params'] = torch.cat([self.data['train_params'], params[train_idx]], dim=0)
        self.data['valid_data'] = torch.cat([self.data['valid_data'], data[valid_idx]], dim=0)
        self.data['valid_params'] = torch.cat([self.data['valid_params'], params[valid_idx]], dim=0)

    def simulate(self, params):
        # TODO: Clean up numpy types
        if type(params) is np.ndarray:
            params = torch.from_numpy(params).float().to(self.device)

        print("in simulate()")
        params = params.reshape([-1, self.param_dim])
        params = torch.cat(self.sims_per_model*[params])

        data = self.simulator(params, sims_per_model=self.sims_per_model)
        if type(data) is np.ndarray:
            data = torch.from_numpy(data)
        data = data.reshape([-1, self.data_dim])
        assert params.shape[0] == data.shape[0], print(params.shape, data.shape)

        return data, params

    def make_loaders(self):
        train_dset = torch.utils.data.TensorDataset(
            self.data['train_data'].float(),
            self.data['train_params'].float())
        train_loader = torch.utils.data.DataLoader(
            train_dset, batch_size=self.batch_size, shuffle=True, drop_last=True)

        valid_dset = torch.utils.data.TensorDataset(
            self.data['valid_data'].float(),
            self.data['valid_params'].float())
        valid_loader = torch.utils.data.DataLoader(
            valid_dset, batch_size=self.batch_size, shuffle=False, drop_last=True)

        return train_loader, valid_loader

    def train(self):
        print(f"Training on {self.data['train_data'].shape[0]:,d} samples. "
              f"Validating on {self.data['valid_data'].shape[0]:,d} samples.")
        train_loader, valid_loader = self.make_loaders()

        self.model.train()
        global_step = 0
        total_loss = 0
        best_val_loss = np.inf
        epochs_without_improvement = 0
        # Train
        pbar = tqdm(range(self.max_n_epochs))
        for epoch in pbar:
            for data, params in train_loader:
                self.optimizer.zero_grad()
                loss = self.model._loss(data.to(self.device), params.to(self.device))
                loss.backward()
                total_loss += loss.item()
                nn.utils.clip_grad_norm_(self.model.parameters(), self.grad_clip)
                self.optimizer.step()
                global_step += 1
            # Evaluate
            self.model.eval()
            with torch.no_grad():
                total_loss = 0
                for i, (data, params) in enumerate(valid_loader):
                    loss = self.model._loss(data.to(self.device), params.to(self.device))
                    total_loss += loss.item()
            val_loss = total_loss / float(1+len(valid_loader))
            if val_loss < best_val_loss:
                with open(self.model_path, 'wb') as f:
                    torch.save(self.model.state_dict(), f)
                best_val_loss = val_loss
            else:
                epochs_without_improvement += 1
            pbar.set_description(f"Validation Loss: {val_loss:.3f}")
            self.model.train()

            if epochs_without_improvement > self.patience:
                print(f"Early stopped after {epoch} epochs")
                break

    def log_prior(self, params):
        return self.priors.log_prob(params)

    def log_posterior(self, params, prior_only=False):
        if type(params) is np.ndarray:
            params = torch.from_numpy(params).float().to(self.device)

        log_prob = self.log_prior(params)
        if not prior_only:
            params = torch.stack(self.x0.shape[0]*[params])
            log_prob = log_prob + self.model.log_prob(self.x0, params)
        return log_prob

    def hmc(self, num_samples=50, walker_steps=200, burn_in=100):
        def model_wrapper(param_dict):
            if param_dict is not None:
                # TODO: Figure out if there's a way to pass params without dict
                log_prob = self.log_prior(param_dict['params'].to(self.device))
                log_prob +=  self.model.log_prob(self.x0, param_dict['params'].to(self.device))
                return -log_prob

        initial_params = self.priors.sample((1, ))
        nuts_kernel = NUTS(potential_fn=model_wrapper, adapt_step_size=True)
        mcmc = MCMC(nuts_kernel, num_samples=walker_steps, warmup_steps=burn_in, initial_params={"params": initial_params})
        mcmc.run(self.x0)
        return mcmc.get_samples(num_samples)['params'].view(num_samples, -1)

    def sample_prior(self, num_samples=1000, prior_only=True):
        if prior_only:
            prior_samples = self.priors.sample((num_samples, ))
        else:  # sample from nde
            prior_samples = self.hmc(num_samples=num_samples)

        if type(prior_samples) is np.ndarray:
            prior_samples = torch.from_numpy(prior_samples).float().to(self.device)

        return prior_samples

    def sample_posterior(self, num_samples=1000, walker_steps=200, burn_in=100):
        samples = self.hmc(num_samples=num_samples, walker_steps=walker_steps, burn_in=burn_in)
        return samples

    def run(self, show_plots=True):
        # TODO: think of way to take out simulator from this loop when simulator not included
        snl_start = time.time()
        for r in range(self.n_rounds):
            round_start = time.time()
            # sample from nde after first round
            if r == 0:
                prior_samples = self.sample_prior(num_samples=self.num_initial_samples,
                                                  prior_only=True)
            else:
                prior_samples = self.sample_prior(num_samples=self.num_samples_per_round,
                                                  prior_only=False)
            if show_plots:
                self.make_plots()

            # Simulate
            sims, prior_samples = self.simulate(prior_samples)
            # Store data
            self.add_data(sims, prior_samples)

            # Train flow on new + old simulations
            self.train()

            t = time.time() - round_start
            total_t = time.time() - snl_start
            print(f"Round {r+1} complete. Time elapsed: {t//60:.0f}m {t%60:.0f}s. "
                  f"Total time elapsed: {total_t//60:.0f}m {total_t%60:.0f}s.")
            print("===============================================================")

    def make_plots(self):
        pass