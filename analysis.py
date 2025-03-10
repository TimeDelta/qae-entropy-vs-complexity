import numpy as np
from qiskit.quantum_info import partial_trace, entropy

def differential_entropy(data, num_bins=None):
    """
    data (np.ndarray): shape should be (num_features, sequence_length)
    num_bins (int or sequence)
    """
    entropy_per_feature
    for f in range(len(data[0])):
        if not num_bins: # default to Freedman-Diaconis Rule due to non-normal data
            q75, q25 = np.percentile(data, [75 ,25])
            IQR = q75 - q25
            bin_width = 2 * IQR / np.cbrt(len(data))
            num_bins = int(np.ceil((np.max(data) - np.min(data)) / bin_width))
        hist, edges = np.histogramdd(data, bins=num_bins, density=True)

        dimensions = data.shape[1]

        # compute bin volumes, assuming uniform bin widths for simplicity (difference between
        # first two bin edges)
        bin_widths = [edges[i][1] - edges[i][0] for i in range(dimensions)]
        bin_volume = np.prod(bin_widths)

        nonzero = hist > 0
        de = -np.sum(hist[nonzero] * np.log(hist[nonzero])) * bin_volume
    return de

def entanglement_entropy(state, subsystem):
    total_qubits = state.num_qubits
    trace_out = [i for i in range(total_qubits) if i not in subsystem]
    reduced_state = partial_trace(state, trace_out)
    return entropy(reduced_state, base=2)

# TODO: better method for deciding number of symbols
# TODO: look into quantizing states as a whole (recursive binning maybe?) instead of per feature
def quantize_signal(data, num_symbols=30):
    """
    Returns: list of integer symbols representing the quantized signal
    """
    if data.ndim > 1:
        print('!! WARNING !!: flattening multidimensional states')
        data = data.ravel()
    data_min, data_max = np.min(data), np.max(data)
    if data_max == data_min: # edge case: all data equal -> avoid /0
        quantized = np.zeros_like(data, dtype=int)
    else:
        quantized = np.floor((data - data_min) / (data_max - data_min) * num_symbols).astype(int)
        quantized[quantized == num_symbols] = num_symbols - 1  # handle edge case
    return quantized.tolist()

def lempel_ziv_complexity_continuous(data, num_symbols=30):
    symbol_seq = quantize_signal(data, num_symbols)
    i = 0
    complexity = 0
    while i < len(symbol_seq):
        l = 1
        # get all substrings of length l starting from index 0 up to i
        strings_so_far = {tuple(symbol_seq[k:k+l]) for k in range(i)}
        while i + l <= len(symbol_seq) and tuple(symbol_seq[i:i+l]) in strings_so_far:
            l += 1
            strings_so_far = {tuple(symbol_seq[k:k+l]) for k in range(i)}
        complexity += 1
        i += l
    return complexity

def _hurst_exponent_1d(data, window_sizes):
    """
    slope of log-log regression
    """
    RS = []
    for window in window_sizes:
        n_segments = len(symbol_seq) // window
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

def hurst_exponent(data, window_space_method='logspace'):
    """
    Parameters:
        data (np.ndarray): shape should be (n_samples, n_features)
        window_space_method (str): 'linspace' or 'logspace'

    Returns: dictionary mapping each feature index to its Hurst exponent.
    """
    # compute separately for each feature (column)
    n_samples, n_features = data.shape
    hurst_vals = {}
    for f_i in range(n_features):
        col = data[:, f_i]
        # use logspace for mixed local / ranged correlation structure
        window_sizes = np.unique(np.floor(np.logspace(np.log10(10), np.log10(n_samples // 2), num=20)).astype(int))
        window_sizes = window_sizes[window_sizes > 0]
        hurst_vals[f_i] = _hurst_exponent_1d(col, window_sizes)
    return hurst_vals

def per_patient(func, data, **kwargs):
    final_values = []
    for p in range(data.shape[0]):
        final_values.append(func(data[p], **kwargs))
    return final_values
