import math
import numpy as np
import os
import torch
from fbm import fbm

from analysis import lempel_ziv_complexity_continuous, hurst_exponent, higuchi_fractal_dimension

def blend_with_new_block(existing_series, new_block, taper_length):
    """
    Linearly taper the last `taper_length` states of existing_series and the first `taper_length` of new_block
    then concatenate them
    """
    if existing_series.shape[0] < taper_length:
        raise ValueError("Existing series is too short to blend")

    if isinstance(existing_series, torch.Tensor):
        existing_series = existing_series.detach().cpu().numpy()
    if isinstance(new_block, torch.Tensor):
        new_block = new_block.detach().cpu().numpy()
    existing_taper = existing_series[-taper_length:]
    new_taper = new_block[:taper_length]

    weight_existing = np.linspace(1, 0, taper_length)[:, None] # column vector
    weight_new = np.linspace(0, 1, taper_length)[:, None]
    existing_taper *= weight_existing
    new_taper *= weight_new

    return np.concatenate((existing_series[:-taper_length], existing_taper, new_taper, new_block[taper_length:]), axis=0)

class FractionalGaussianSequenceGenerator:
    """
    Generates a time series where each feature is produced by fractional Gaussian noise with a specified Hurst exponent.
    """
    def __init__(self, num_features, num_states):
        self.num_features = num_features
        self.num_states = num_states

    def forward(self, mean, stdev, hurst_target):
        # generates one-dimensional fractional Brownian motion
        series = []
        for f in range(self.num_features):
            # fbm() returns an array of length n+1: subtract first value so series starts at 0
            FBM = fbm(n=self.num_states-1, hurst=hurst_target, length=1, method='daviesharte')
            FBM = FBM - FBM[0]
            # scale and shift the series by stdev and mean for feature f
            series.append(mean[f] + stdev[f] * np.tanh(FBM))
        return np.stack(series, axis=-1).astype(np.float32)

base_dir = 'generated_datasets'
if not os.path.exists(base_dir):
    os.makedirs(base_dir)

num_features_per_state = 8 # num_qubits
num_series_per_dataset = 20
num_blocks_per_series = 5
num_states_per_block = 20
num_time_steps_to_taper = num_states_per_block // 10
num_datasets = 250
required_length = num_blocks_per_series * num_states_per_block
dset_hurst_min = .9
dset_hurst_max = 1

datasets = []
generator = FractionalGaussianSequenceGenerator(num_features_per_state, num_states_per_block)
for d in range(num_datasets):
    print('Generating dataset ' + str(d + 1))
    dataset_dir = os.path.join(base_dir, f'dataset_{d+1}')
    if not os.path.exists(dataset_dir):
        os.makedirs(dataset_dir)

    generated_sequences = []
    orig_mean = np.random.uniform(-10, 10, size=(num_features_per_state,))
    upper_bounds = np.maximum(np.abs(orig_mean) / 2, 1)
    orig_stdev = np.random.uniform(1, upper_bounds)
    dset_hurst_min -= (1/num_datasets) * dset_hurst_min
    dset_hurst_max -= (1/num_datasets) * dset_hurst_max
    print('  target hurst exponent min:', dset_hurst_min)
    print('  target hurst exponent max:', dset_hurst_max)

    for i in range(num_series_per_dataset):
        print('  Generating series ' + str(i + 1))
        hurst_target = np.random.uniform(dset_hurst_min, dset_hurst_max)
        print('    chosen target hurst exponent:', hurst_target)
        series = []
        for b in range(num_blocks_per_series):
            # have to blend multiple series together to ensure non-stationarity
            percentage = math.sin((num_blocks_per_series - b + 1) / num_blocks_per_series * math.pi - math.pi/2)
            mean = orig_mean * percentage * np.linspace(.5, 1, num_features_per_state)
            stdev = orig_stdev * percentage * np.linspace(.5, 1, num_features_per_state)
            new_block = generator.forward(mean, stdev, hurst_target)
            if len(series) > 0:
                series = blend_with_new_block(series, new_block, num_time_steps_to_taper)
            else:
                series = new_block
        if isinstance(series, torch.Tensor):
            series = series.detach().cpu().numpy()
        elif isinstance(series, list):
            series = np.array(series)
        while series.shape[0] < required_length:
            series = np.concatenate((series, series), axis=0)
        series = series[:required_length]
        generated_sequences.append(series)
        series_filename = os.path.join(dataset_dir, f'series_{i+1}.npy')
        np.save(series_filename, series)
    num_blocks_per_series /= (1 + 1/num_datasets)
    num_blocks_per_series = int(max(num_blocks_per_series, 1))
    datasets.append(generated_sequences)

series_metrics = []
for dataset_index, generated_sequences in enumerate(datasets):
    for series in generated_sequences:
        print('Calculating complexity metrics for series ' + str(len(series_metrics) + 1), '(dataset ' + str(dataset_index + 1) + ')')
        metrics = {
            'lzc': lempel_ziv_complexity_continuous(series),
            'he': np.mean(hurst_exponent(series)),
            'hfd': np.mean(higuchi_fractal_dimension(series)),
            'dataset': dataset_index + 1
        }
        series_metrics.append((metrics, series))

print('Determining which series to keep ...')
num_bins_per_metric = 10

all_metrics = np.array([[m['lzc']] for m, _ in series_metrics])
lzc_vals = all_metrics[:, 0]
min_lzc = np.min(lzc_vals)
max_lzc = np.max(lzc_vals)

lzc_edges = np.linspace(min_lzc, max_lzc, num_bins_per_metric + 1)
he_edges = np.linspace(0, 1, num_bins_per_metric + 1)
hfd_edges = np.linspace(1, 2, num_bins_per_metric + 1)

series_metric_grid = {}
for metrics, series in series_metrics:
    lzc, he, hfd = metrics['lzc'], metrics['he'], metrics['hfd']
    if np.isnan(he) or np.isnan(hfd):
        print('WARNING: Invalid complexity metric value: ', he, hfd)
        continue
    lzc_bin = np.digitize(lzc, lzc_edges) - 1
    he_bin = np.digitize(he, he_edges) - 1
    hfd_bin = np.digitize(hfd, hfd_edges) - 1
    metrics_key = (lzc_bin, he_bin, hfd_bin)
    # only store first encountered series per cell (this also reduces the number of models needed to be trained
    # since the series appear in order of dataset)
    if metrics_key not in series_metric_grid:
        series_metric_grid[metrics_key] = (metrics, series)
        print(metrics)
        filename = f'series_cell_{metrics_key[0]}_{metrics_key[1]}_{metrics_key[2]}_dataset{metrics["dataset"]}.npy'
        np.save(os.path.join(base_dir, filename), series)

print('Number of grid cells covered: ', len(series_metric_grid))
