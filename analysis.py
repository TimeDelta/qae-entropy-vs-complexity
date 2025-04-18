import numpy as np
import numpy.fft as fft
import torch
from qiskit.quantum_info import partial_trace, entropy
import antropy
from astropy.stats import bayesian_blocks
import matplotlib.pyplot as plt

from typing import Dict
import random
from dataclasses import dataclass, field
import colorsys
import os

from data_importers import import_generated

num_states_per_block = 5
LOSS_TYPES = ['Prediction', 'Bottleneck Trash']
MODEL_TYPES = ['qae', 'qrae', 'qte', 'qrte', 'cae', 'crae', 'cte', 'crte']
# q = quantum; c = classical
# r = recurrent
# ae = auto-encoder; te = transition encoder (auto-regressive)

def check_for_overfitting(training_costs, validation_costs, threshold=.15):
    """
    each cost part must already be normalized by number of series in that partition
    """
    def check_overfit(tc, vc, loss_type):
        overfit_ratio = (vc-tc)/tc
        if overfit_ratio > threshold:
            print(f'WARNING: Overfit likely based on {loss_type} costs (validation higher by {(100*overfit_ratio):.1f}%)')
            return True
        return False
    for tc, vc, loss_type in zip(training_costs, validation_costs, LOSS_TYPES):
        check_overfit(tc, vc, loss_type)

    total_training_cost = np.sum(training_costs)
    total_validation_cost = np.sum(validation_costs)
    return check_overfit(total_training_cost, total_validation_cost, 'Total')

def multimodal_differential_entropy_per_feature(data):
    """
    data (np.ndarray): shape should be (sequence_length, num_features)
    Uses adaptive width per bin to allow for multimodality in underlying series.
    """
    entropy_per_feature = []
    num_features = data.shape[1]

    for f in range(num_features):
        feature_data = data[:, f]
        # use bayesian_blocks from astropy to adaptively determine bin edges
        # use density normalization so that the integral is one
        hist, edges = np.histogram(feature_data, bins=bayesian_blocks(feature_data), density=True)

        bin_widths = np.diff(edges) # 1D differences from the histogram edges
        bin_prob_mass = hist * bin_widths

        nonzero = bin_prob_mass > 0
        de = -np.sum(bin_prob_mass[nonzero] * np.log(bin_prob_mass[nonzero]))
        entropy_per_feature.append(de)
    return entropy_per_feature

def von_neumann_entropy(dm, log_base=2) -> float:
    dm_eigenvalues = np.linalg.eigvalsh(dm.data)
    dm_eigenvalues = dm_eigenvalues[dm_eigenvalues > 1e-12] # for stability
    return -np.sum(dm_eigenvalues * np.log(dm_eigenvalues)) / np.log(log_base)

def entanglement_entropy(state):
    """
    Computes the Meyer-Wallach global entanglement measure for an n-qubit pure state as
    (2/n) * sum_{r=1}^{n} [1 - Tr(dm_r^2)]
    where dm_r is the reduced density matrix of qubit r
    """
    n = state.num_qubits
    total = 0.0

    for r in range(n):
        trace_out = [i for i in range(n) if i != r]
        reduced_state = partial_trace(state, trace_out)
        # purity = Tr(dm_r^2); (1 - purity) is linear entropy of this qubit
        total += (1 - reduced_state.purity())

    # Meyer-Wallach normalizes by the number of qubits
    return (2 / n) * total

# TODO: better method for deciding number of symbols
def quantize_signal(data, num_symbols=30):
    if isinstance(data, torch.Tensor):
        data = data.detach().cpu().numpy()
    if data.ndim == 1:
        data_min, data_max = np.min(data), np.max(data)
        if data_max == data_min:
            quantized = np.zeros_like(data, dtype=int)
        else:
            quantized = np.floor((data - data_min) / (data_max - data_min) * num_symbols).astype(int)
            quantized[quantized == num_symbols] = num_symbols - 1 # enforce symbol bounds
        return quantized.tolist()
    elif data.ndim == 2:
        n_samples, n_features = data.shape
        quantized_features = []
        for i in range(n_features): # quantize each feature separately
            channel = data[:, i]
            channel_min, channel_max = np.min(channel), np.max(channel)
            if channel_max == channel_min:
                quanta = np.zeros_like(channel, dtype=int)
            else:
                quanta = np.floor((channel - channel_min) / (channel_max - channel_min) * num_symbols).astype(int)
                quanta[quanta == num_symbols] = num_symbols - 1
            quantized_features.append(quanta)
        quantized_features = np.stack(quantized_features, axis=1) # shape (n_samples, n_features)
        # combine features using mixed-radix encoding (treat each feature’s quantized value as a digit in a number with base equal to num_symbols)
        composite_symbols = np.sum(quantized_features * (num_symbols ** np.arange(n_features)), axis=1)
        return composite_symbols.tolist()
    else:
        raise ValueError("Data must be 1D or 2D.")

def lempel_ziv_complexity_continuous(data, num_symbols=30):
    symbol_seq = quantize_signal(data, num_symbols)
    phrase_start = 0
    complexity = 0

    while phrase_start < len(symbol_seq):
        phrase_length = 1
        while True:
            # so that a substring of target phrase length sits entirely before phrase_start
            max_prefix_start = phrase_start - phrase_length + 1

            if max_prefix_start > 0:
                # all substrings of phrase_length in the prefix [0 : phrase_start]
                previous_substrings = {
                    tuple(symbol_seq[k : k + phrase_length])
                    for k in range(max_prefix_start)
                }
            else:
                previous_substrings = set()

            end_of_candidate = phrase_start + phrase_length

            # does it still perfectly match something in the prefix?
            if (
                end_of_candidate <= len(symbol_seq)
                and tuple(symbol_seq[phrase_start : end_of_candidate])
                in previous_substrings
            ):
                phrase_length += 1
                continue
            else:
                break
        complexity += 1
        phrase_start += phrase_length
    return complexity

def _hurst_exponent_1d(data, window_sizes):
    """
    slope of log-log regression
    """
    RS = []
    for window in window_sizes:
        n_segments = len(data) // window
        RS_vals = []
        for i in range(n_segments):
            segment = data[i * window:(i + 1) * window]
            mean_seg = np.mean(segment)
            Y = segment - mean_seg
            cumulative_Y = np.cumsum(Y)
            R = np.max(cumulative_Y) - np.min(cumulative_Y)
            S = np.std(segment)
            if S != 0:
                RS_vals.append(R / S)
        if RS_vals:
            RS.append(np.mean(RS_vals))
    if len(RS) == 0:
        raise ValueError("No valid RS values computed; check window sizes and data.")
    logs = np.log(window_sizes[:len(RS)])
    log_RS = np.log(RS)
    slope, _ = np.polyfit(logs, log_RS, 1)
    return slope

def hurst_exponent(data):
    # compute separately for each feature (column)
    n_samples, n_features = data.shape
    hurst_vals = []
    for f_i in range(n_features):
        col = data[:, f_i]
        # use logspace for mixed local / ranged correlation structure
        window_sizes = np.unique(np.floor(np.logspace(np.log10(10), np.log10(n_samples // 2), num=20)).astype(int))
        window_sizes = window_sizes[window_sizes > 0]
        hurst_vals.append(_hurst_exponent_1d(col, window_sizes))
    return hurst_vals

def higuchi_fractal_dimension(data, kmax=10):
    n_samples, n_features = data.shape

    hfds = []
    for feature in range(n_features):
        feature_series = data[:, feature]
        try:
            hfd = antropy.higuchi_fd(feature_series, kmax=kmax)
        except Exception as e:
            hfd = np.nan
            print('HFD NaN for kmax of ', kmax, ':', feature_series.shape)
        hfds.append(hfd)
    return hfds

def per_patient(func, data, **kwargs):
    final_values = []
    for p in range(data.shape[0]):
        final_values.append(func(data[p], **kwargs))
    return final_values

def run_analysis(datasets, data_dir, overfit_threshold):
    num_training_series = len(next(iter(datasets.values()))[0])
    print(num_training_series)
    num_validation_series = len(next(iter(datasets.values()))[1])
    print(num_validation_series)

    # lambda to parse each individual series into a single value
    # lambda to aggregate all series of a dataset into a single value
    MODEL_MEAN_MEAN_STAT_LAMBDAS = (
        lambda rows: {row[0]: np.mean(row[1:]) for row in rows},
        lambda rows: np.mean([np.mean(row[1:]) for row in rows])
    )
    MODEL_MEAN_SUM_STAT_LAMBDAS = (
        lambda rows: {row[0]: np.sum(row[1:]) for row in rows},
        lambda rows: np.mean([np.sum(row[1:]) for row in rows])
    )
    MODEL_MEAN_PLAIN_STAT_LAMBDAS = (
        lambda rows: {row[0]: row[1] for row in rows},
        lambda rows: np.mean([row[1] for row in rows])
    )
    MODEL_STATS_CONFIG = {
        # cost_history gets added separately
        'validation_costs': {
            LOSS_TYPES[i]: (
                lambda rows: {row[0]: row[i+1] for row in rows},
                lambda rows: np.mean([row[i+1] for row in rows])
            ) for i in range(len(LOSS_TYPES))
        },
        'bottleneck_differential_entropy': MODEL_MEAN_SUM_STAT_LAMBDAS,
        'bottleneck_entanglement_entropy': MODEL_MEAN_SUM_STAT_LAMBDAS,
        'bottleneck_full_vn_entropy': MODEL_MEAN_SUM_STAT_LAMBDAS,
        'bottleneck_lzc': MODEL_MEAN_PLAIN_STAT_LAMBDAS,
        'bottleneck_he': MODEL_MEAN_MEAN_STAT_LAMBDAS,
        'bottleneck_hfd': MODEL_MEAN_MEAN_STAT_LAMBDAS,
    }
    MODEL_STATS_CONFIG['validation_costs']['Total'] = (
        lambda rows: {row[0]: sum(row[1:]) for row in rows},
        lambda rows: np.mean([sum(row[1:]) for row in rows])
    )
    SERIES_STATS_CONFIG = {
        # lambda to map series into single value
        'hurst_exponent':            lambda series: np.mean(hurst_exponent(series)),
        'lempel_ziv_complexity':     lambda series: lempel_ziv_complexity_continuous(series),
        'higuchi_fractal_dimension': lambda series: np.mean(higuchi_fractal_dimension(series)),
        'differential_entropy':      lambda series: np.mean(multimodal_differential_entropy_per_feature(series)),
    }
    MAPPINGS_TO_PLOT = { # {series_attribute: [model_attribute]}
        'hurst_exponent': ['bottleneck_he', 'bottleneck_entanglement_entropy', 'bottleneck_full_vn_entropy'],
        'lempel_ziv_complexity': ['bottleneck_lzc', 'bottleneck_entanglement_entropy', 'bottleneck_full_vn_entropy'],
        'higuchi_fractal_dimension': ['bottleneck_hfd', 'bottleneck_entanglement_entropy', 'bottleneck_full_vn_entropy'],
        'differential_entropy': ['bottleneck_differential_entropy', 'bottleneck_entanglement_entropy', 'bottleneck_full_vn_entropy'],
    }
    independent_keys = list(SERIES_STATS_CONFIG.keys())
    dependent_keys = []
    for dependent_var_key in MODEL_STATS_CONFIG.keys():
        parsers = MODEL_STATS_CONFIG[dependent_var_key]
        if isinstance(parsers, Dict):
            dependent_keys.extend(parsers.keys())
        else:
            dependent_keys.append(dependent_var_key)

    @dataclass
    class ModelStats:
        data: dict = field(default_factory=dict)

        def load(self, dataset_index, model_type, dsets_dir, run_prefix):
            for key in MODEL_STATS_CONFIG.keys():
                if model_type.startswith('c') and ('vn' in key or 'entangle' in key):
                    continue
                filepath = os.path.join(dsets_dir, f'{run_prefix}dataset{dataset_index}_{model_type}_{key}.npy')
                self.data[key] = np.load(filepath)
                if key == 'validation_costs':
                    for k in MODEL_STATS_CONFIG[key].keys():
                        self.data[k] = self.data[key]
            filepath = os.path.join(dsets_dir, f'{run_prefix}dataset{dataset_index}_{model_type}_cost_history.npy')
            self.data['cost_history'] = np.load(filepath)

    @dataclass
    class SeriesStats:
        data: dict = field(default_factory=dict)

        def compute(self, series):
            for key, func in SERIES_STATS_CONFIG.items():
                self.data[key] = func(series)

    bar = '=-=-=-=-=-=-=-=-=-=-=-=-=-=-='

    # load model statistics for each dataset and model type (qae and qte)
    stats_per_model = {}
    for d_i in datasets:
        dataset_stats = {}
        skip_dset = False # for testing purposes
        for model_type in MODEL_TYPES:
            print(f'Loading {model_type} model statistics for dataset {d_i}')
            stats = ModelStats()
            try:
                stats.load(d_i, model_type, data_dir, run_prefix)
                dataset_stats[model_type] = stats
                mean_training_costs = stats.data['cost_history'][-1] / num_training_series
                mean_validation_costs = np.sum(stats.data['validation_costs'][:,1:], axis=0) / num_validation_series
                check_for_overfitting(mean_training_costs, mean_validation_costs, overfit_threshold)
            except Exception as e:
                if args.test:
                    print('  skipping due to exception: ' + str(e))
                    skip_dset = True # skip entire dataset to ensure homogenous array dimensions
                    break
                else:
                    raise e
        if not skip_dset:
            for model_type in MODEL_TYPES:
                stats_per_model[(d_i, model_type)] = dataset_stats[model_type]

    print('\n\n\n' + bar)

    # compute complexity metrics for all validation series
    dataset_series_stats = {}
    for d_i, (training_series, validation_series) in datasets.items():
        for s_i, series in validation_series:
            num_features = len(series[0])
            print(f'Computing complexity metrics for dataset {d_i} series {s_i} ({num_features} features)')
            series_stats = SeriesStats()
            series_stats.compute(series)
            # store as tuple (s_i, series_stats) for later annotation
            dataset_series_stats.setdefault(d_i, []).append((s_i, series_stats))

    individual_plot_data = {i_key: {d_key: {model: [] for model in MODEL_TYPES} for d_key in dependent_keys} for i_key in independent_keys}
    aggregated_plot_data = {i_key: {d_key: {model: [] for model in MODEL_TYPES} for d_key in dependent_keys} for i_key in independent_keys}

    for (d_i, model_type), m_stats in stats_per_model.items():
        dependent_individual = {}
        dependent_aggregated = {}
        for dependent_var_key in MODEL_STATS_CONFIG.keys():
            parsers = MODEL_STATS_CONFIG[dependent_var_key]
            if isinstance(parsers, Dict): # allow multiple values from same file
                for key, (parse_individual, parse_aggregated) in parsers.items():
                    dependent_individual[key] = parse_individual(m_stats.data[key])
                    dependent_aggregated[key] = parse_aggregated(m_stats.data[key])
            else:
                (parse_individual, parse_aggregated) = parsers
                dependent_individual[dependent_var_key] = parse_individual(m_stats.data[key])
                dependent_aggregated[dependent_var_key] = parse_aggregated(m_stats.data[key])

        # Get the series stats for the current dataset.
        series_stats_list = dataset_series_stats[d_i]

        num_entropy_warnings = 0
        for s_i, s_stats in series_stats_list:
            for i_key in independent_keys:
                for d_key in dependent_keys:
                    individual_plot_data[i_key][d_key][model_type].append((s_stats.data[i_key], dependent_individual[d_key][s_i], d_i, s_i))
                    aggregated_plot_data[i_key][d_key][model_type].append((s_stats.data[i_key], dependent_aggregated[d_key], d_i, s_i))

            ent_entropy = dependent_individual['bottleneck_entanglement_entropy'].get(s_i)
            full_vn = dependent_individual['bottleneck_full_vn_entropy'].get(s_i)
            if ent_entropy is None:
                raise Exception(f'ERROR: missing entanglement entropy for dataset {d_i} series {s_i} {model_type} model')
            if full_vn is None:
                raise Exception(f'ERROR: missing full VN entropy for dataset {d_i} series {s_i} {model_type} model')
            if full_vn < ent_entropy - 1E-15:
                print(f'WARNING: full VN entropy < entanglement entropy by {abs(full_vn - ent_entropy)} for dataset {d_i} series {s_i}')
                num_entropy_warnings += 1
        if num_entropy_warnings > 0:
            percent = num_entropy_warnings / len()
            print(f'{num_entropy_warnings} total warnings ({percent}%) for unexpected quantum entropy relationship w/ dataset {d_i} {model_type}')

    """
    Generate 'number_of_colors' distinct colors using the HSV (HSB) color space.
    Each color is evenly spaced in hue, with a random brightness:
    - If the system background is white (e.g., a light theme), brightness is chosen randomly between 0.4 and 0.8.
    - Otherwise, brightness is chosen randomly between 0.6 and 1.0.
    * Based on my code at https://github.com/TimeDelta/introspective/blob/32af5154e2c6bd0bc4c7196d44f76076281abebc/Introspective/UI/Graphing/GraphDataGenerators/XYGraphDataGenerator.swift#L299
    """
    colormap_colors = []
    for color_index in range(len(MODEL_TYPES)):
        hue_value = float(color_index) / len(MODEL_TYPES) # even spacing across the hue spectrum

        brightness_range_low = 0.4
        brightness_range_high = 0.8

        brightness_value = random.uniform(brightness_range_low, brightness_range_high)
        saturation_value = 1.0

        red_value, green_value, blue_value = colorsys.hsv_to_rgb(hue_value, saturation_value, brightness_value)
        colormap_colors.append((red_value, green_value, blue_value))
    colors = {model: color for (model, color) in zip(MODEL_TYPES, colormap_colors)}

    def plot_data(data_dict, x_label, y_label, title, filename):
        def plot_model_data_and_save(data, label_prefix, color):
            x_vals = [d[0] for d in data]
            y_vals = [d[1] for d in data]
            plt.scatter(x_vals, y_vals, color=color, s=5)

            coeffs = np.polyfit(x_vals, y_vals, 1)
            slope = float(coeffs[0])
            poly_eqn = np.poly1d(coeffs)
            x_fit = np.linspace(min(x_vals), max(x_vals), 100)
            plt.plot(x_fit, poly_eqn(x_fit), color=color, linestyle='--', label=f'{label_prefix} (slope={slope:.5f})')

        plt.figure()
        if isinstance(next(iter(data_dict.values())), dict):
            for loss_type, model_data in data_dict.items():
                for model_type, data in model_data.items():
                    label_prefix = f"{model_type.upper()}-{loss_type}"
                    plot_model_data_and_save(data, label_prefix, colors[model_type])
        else:
            for model_type, data in data_dict.items():
                label_prefix = model_type.upper()
                plot_model_data_and_save(data, label_prefix, colors[model_type])
        plt.xlabel(x_label)
        plt.ylabel(y_label)
        plt.title(title)
        plt.legend()
        save_path = os.path.join(data_dir, run_prefix + filename)
        plt.savefig(save_path)

    print('\n\n\n' + bar)

    # TODO: need to plot classical and quantum losses sepoarately due to scale differences
    for (i_key, dependent_keys) in MAPPINGS_TO_PLOT.items():
        for d_key in dependent_keys:
            x_label = i_key.replace('_', ' ').title()
            y_label = d_key.replace('_', ' ').title()
            print(f'Plotting individual data for {y_label} vs {x_label}')
            plot_data(
                data_dict=individual_plot_data[i_key][d_key],
                x_label=f'{x_label} (Individual)',
                y_label=f'{y_label} (Validation)',
                title=f'{x_label} vs {y_label} Per Series',
                filename=f'{run_prefix}{d_key}_vs_{i_key}_individual.png'
            )

            print(f'Plotting aggregated data for {y_label} vs {x_label}')
            plot_data(
                data_dict=aggregated_plot_data[i_key][d_key],
                x_label=f'Mean {x_label}',
                y_label=f'{y_label} (Validation)',
                title=f'Mean {x_label} vs {y_label} Per Dataset',
                filename=f'{run_prefix}{d_key}_vs_{i_key}_aggregated.png'
            )

    # aggregate cost history for each model type across datasets
    cost_history_by_model = {m: [] for m in MODEL_TYPES}
    for (dataset_index, model_type), model_stats in stats_per_model.items():
        cost_history_by_model[model_type].append(model_stats.data['cost_history'])
    mean_cost_history_per_model_type = {}
    for (model_type, cost_history_list) in cost_history_by_model.items():
        cost_history_arrays = np.array(cost_history_list) # shape: (num_runs, num_epochs, num_loss_types)
        if cost_history_arrays.ndim >= 3 and cost_history_arrays.shape[0] > 1:
            mean_cost_history = cost_history_arrays.mean(axis=0)
        elif cost_history_arrays.shape[0] == 1:
            mean_cost_history = cost_history_arrays[0]
        elif args.test:
            print(f'WARNING: Missing {model_type} cost history')
            continue
        else:
            raise Exception(f'Unable to find any {model_type} model cost history')
        mean_cost_history_per_model_type[model_type] = mean_cost_history

    sample = next(iter(mean_cost_history_per_model_type.values()))
    num_epochs, num_loss_types = sample.shape

    # randomly sample 10% datasets to plot individual histories per model type to avoid too much clutter
    individual_datasets_to_plot = list(set(d_i for (d_i, model_type) in stats_per_model.keys()))
    random.shuffle(individual_datasets_to_plot)
    individual_datasets_to_plot = individual_datasets_to_plot[:max(len(individual_datasets_to_plot)//10, 1)]
    for cost_part_index in range(num_loss_types):
        plt.figure()
        for (d_i, model_type), model_stats in stats_per_model.items():
            if d_i not in individual_datasets_to_plot:
                continue
            cost_series = model_stats.data['cost_history'][:, cost_part_index]
            plt.plot(range(len(cost_series)), cost_series, label=model_type.upper(), color=colors[model_type])

        loss_label = LOSS_TYPES[cost_part_index]
        plt.xlabel('Epoch')
        plt.ylabel(f'{loss_label} Loss')
        plt.title(f'Sample {loss_label} Loss Histories')
        plt.legend()
        save_filepath = os.path.join(data_dir, f'{run_prefix}{model_type}_sample_{loss_label.replace(" ", "_").lower()}_cost_histories.png')
        plt.savefig(save_filepath)
        print(f'Saved cost history plot for {loss_label} to {save_filepath}')

    # plot cost history for each cost part separately
    for cost_part_index in range(num_loss_types):
        plt.figure()
        for model_type, cost_history in mean_cost_history_per_model_type.items():
            epoch_indices = np.arange(num_epochs)
            cost_series = cost_history[:, cost_part_index]
            plt.plot(epoch_indices, cost_series, label=model_type.upper(), color=colors[model_type])

        loss_label = LOSS_TYPES[cost_part_index]
        plt.xlabel('Epoch')
        plt.ylabel(f'Mean {loss_label} Loss')
        plt.title(f'Mean {loss_label} Loss History per Model Type')
        plt.legend()

        save_filepath = os.path.join(data_dir, f'{run_prefix}_mean_{loss_label.replace(" ", "_").lower()}_cost_histories.png')
        plt.savefig(save_filepath)
        print(f'Saved cost history plot for {loss_label} to {save_filepath}')

    plt.show()

    # precompute and cache 1st/2nd derivatives, FFTs per model/loss type combo
    model_computation_cache = {}
    for model_type in MODEL_TYPES:
        cost_history = mean_cost_history_per_model_type[model_type] # shape: (num_epochs, num_loss_types)

        first_derivatives = np.gradient(cost_history, axis=0)
        model_fft_first = {}
        for cost_part_index in range(num_loss_types):
            model_fft_first[cost_part_index] = fft.fft(first_derivatives[:, cost_part_index])

        second_derivatives = np.gradient(first_derivatives, axis=0)
        model_fft_second = {}
        for cost_part_index in range(num_loss_types):
            model_fft_second[cost_part_index] = fft.fft(second_derivatives[:, cost_part_index])

        frequencies = fft.fftfreq(cost_history.shape[0]) # freqs available based on nyquist sampling
        model_computation_cache[model_type] = {
            'cost_history': cost_history,
            'first_derivatives': first_derivatives,
            'second_derivatives': second_derivatives,
            'fft_first': model_fft_first,
            'fft_second': model_fft_second,
            'frequencies': frequencies
        }

    # mean absolute 1st & 2nd derivatives
    steepness_metrics = {}
    curvature_metrics = {}
    for model in MODEL_TYPES:
        steepness_metrics[model] = {}
        curvature_metrics[model] = {}
        for cost_part_index in range(num_loss_types):
            first = model_computation_cache[model]['first_derivatives'][:, cost_part_index]
            second = model_computation_cache[model]['second_derivatives'][:, cost_part_index]
            steepness_metrics[model][cost_part_index] = np.mean(np.abs(first))
            curvature_metrics[model][cost_part_index] = np.mean(np.abs(second))

    model_types_header = '\t'.join([m.upper().replace('_', ' ') for m in MODEL_TYPES])
    for cost_part_index in range(num_loss_types):
        print(f'\n\n\n{bar}\n{LOSS_TYPES[cost_part_index]} Loss Landscapes:\n{bar}')
        print('  Pairwise Pearson Correlations:')
        print('\t\t' + model_types_header)
        for i, model_i in enumerate(MODEL_TYPES):
            row_values = [model_i.upper()]
            series_i = model_computation_cache[model_i]['cost_history'][:, cost_part_index]
            for j, model_j in enumerate(MODEL_TYPES):
                series_j = model_computation_cache[model_j]['cost_history'][:, cost_part_index]
                corr_coeff = np.corrcoef(series_i, series_j)[0, 1]
                row_values.append(f'{corr_coeff:.3f}')
            print('\t' + '\t'.join(row_values))
        print('  Mean Absolute Slope per Model Type:')
        for model_type in MODEL_TYPES:
            print(f'    {model_type.upper()}: {steepness_metrics[model_type][cost_part_index]:.10f}')
        print('  Mean Absolute 2nd Derivative per Model Type:')
        for model_type in MODEL_TYPES:
            print(f'    {model_type.upper()}: {curvature_metrics[model_type][cost_part_index]:.10f}')

        def compute_and_print_cross_corr_similarity(model_computation_cache, key):
            mean_centered_values = {}
            for model_type in MODEL_TYPES:
                values = model_computation_cache[model_type][key][:, cost_part_index]
                mean_centered_values[model_type] = values - np.mean(values)
            print('\t\t' + model_types_header)
            for i, model_i in enumerate(MODEL_TYPES):
                row_values = [model_i.upper()]
                for j, model_j in enumerate(MODEL_TYPES):
                    x = mean_centered_values[model_i]
                    y = mean_centered_values[model_j]
                    cross_corr = np.correlate(x, y, mode='full')
                    # normalize by product of norms to get similarity metric
                    norm_product = np.linalg.norm(x) * np.linalg.norm(y)
                    similarity = np.max(np.abs(cross_corr)) / norm_product if norm_product > 0 else np.nan
                    row_values.append(f'{similarity:.3f}')
                print('\t' + '\t'.join(row_values))
        print('  Pairwise Max Normalized Cross-Correlation of Mean-Centered 1st Derivatives:')
        compute_and_print_cross_corr_similarity(model_computation_cache, 'first_derivatives')
        print('  ... 2nd Derivatives:')
        compute_and_print_cross_corr_similarity(model_computation_cache, 'second_derivatives')

        # compute PSD over high frequency 1st / 2nd derivatives loss curves to determine
        # "high" frequency cutoff threshold for each one in each part of loss landscape
        energy_cutoff_ratio = 0.95
        for derivative_type in ['first', 'second']:
            # compute aggregated cumulative energy distribution
            aggregated_thresholds = {}
            aggregated_power = None
            frequencies = None
            # threshold is determined by (derivative_order, loss_type) and is the same across model types
            for model_type in MODEL_TYPES:
                fft_result = model_computation_cache[model_type][f'fft_{derivative_type}'][cost_part_index]
                power_spectrum = np.abs(fft_result) ** 2
                if frequencies is None:
                    frequencies = model_computation_cache[model_type]['frequencies']
                if aggregated_power is None:
                    aggregated_power = power_spectrum.copy()
                else:
                    aggregated_power += power_spectrum
            sorted_indices = np.argsort(np.abs(frequencies))
            sorted_freqs = np.abs(frequencies)[sorted_indices]
            sorted_power = aggregated_power[sorted_indices]
            cumulative_energy = np.cumsum(sorted_power)
            total_energy = cumulative_energy[-1]

            # find frequency such that energy_cutoff_ratio of energy is below it
            dynamic_threshold_index = np.searchsorted(cumulative_energy, energy_cutoff_ratio * total_energy)
            threshold = sorted_freqs[dynamic_threshold_index]
            aggregated_thresholds[cost_part_index] = threshold
            print(f'  High Frequency {derivative_type.title()} Derivative Threshold (based on {int(100*energy_cutoff_ratio)}% energy cutoff ratio): {threshold:.4f}')

            # compute the high-frequency energy ratio
            hf_energy_uniform = {}
            for model_type in MODEL_TYPES:
                hf_energy_uniform[model_type] = {}
                fft_result = model_computation_cache[model_type][f'fft_{derivative_type}'][cost_part_index]
                power_spectrum = np.abs(fft_result) ** 2
                frequencies = model_computation_cache[model_type]['frequencies']
                threshold = aggregated_thresholds[cost_part_index]
                high_freq_mask = np.abs(frequencies) > threshold
                high_freq_energy = np.sum(power_spectrum[high_freq_mask])
                total_energy = np.sum(power_spectrum)
                hf_ratio = high_freq_energy / total_energy if total_energy != 0 else np.nan
                hf_energy_uniform[model_type][cost_part_index] = hf_ratio
                print(f'    Model {model_type.upper()}: High Frequency Energy Ratio = {hf_ratio:.4f}')

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(
        description="Train a QTE and QAE and generate correlation plots."
    )
    parser.add_argument("datasets_directory", type=str, nargs='?', default='generated_datasets', help="Path to the directory containing the generated datasets.")
    parser.add_argument("--test", action='store_true', default=False)
    parser.add_argument("--prefix", type=str, default=None, help="Prefix to use for every saved file name in this run")
    parser.add_argument("--overfit_threshold", type=float, default=.15, help="Detection threshold for overfit ratio (max % for increase in validation cost vs training cost)")
    args = parser.parse_args()

    run_prefix = args.prefix if args.prefix else ''
    datasets = import_generated(args.datasets_directory)
    run_analysis(datasets, args.overfit_threshold)
