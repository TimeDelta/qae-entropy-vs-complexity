import numpy as np
from qiskit import QuantumCircuit
from qiskit.circuit import Parameter
from qiskit.quantum_info import Statevector, state_fidelity, partial_trace
import re

ENTANGLEMENT_OPTIONS = ['full', 'linear', 'circular']
ENTANGLEMENT_GATES = ['cx', 'cz', 'rzx']
EMBEDDING_ROTATION_GATES = ['rx', 'ry', 'rz']

def create_embedding_circuit(num_qubits, embedding_gate):
    """
    Apply rotation gate to each qubit for embedding of classical data.
    Returns qc, input_params
    """
    input_params = []
    qc = QuantumCircuit(num_qubits)
    for i in range(num_qubits):
        if embedding_gate.lower() == 'rx':
            p = Parameter('Embedding Rx θ ' + str(i))
            input_params.append(p)
            qc.rx(p, i)
        elif embedding_gate.lower() == 'ry':
            p = Parameter('Embedding Ry θ ' + str(i))
            input_params.append(p)
            qc.ry(p, i)
        elif embedding_gate.lower() == 'rz':
            p = Parameter('Embedding Rz θ ' + str(i))
            input_params.append(p)
            qc.rz(p, i)
        else:
            raise Exception("Invalid embedding gate: " + embedding_gate)
    return qc, input_params

def add_entanglement_topology(qc, num_qubits, entanglement_topology, entanglement_gate):
    if entanglement_topology == 'full':
        for i in range(num_qubits):
            for j in range(i+1, num_qubits):
                if entanglement_gate.lower() == 'cx':
                    qc.cx(i, j)
                elif entanglement_gate.lower() == 'cz':
                    qc.cz(i, j)
                elif entanglement_gate.lower() == 'rzx':
                    qc.rzx(np.pi/4, i, j)
                else:
                    raise Exception("Unknown entanglement gate: " + entanglement_gate)
    elif entanglement_topology == 'linear':
        for i in range(num_qubits - 1):
            if entanglement_gate.lower() == 'cx':
                qc.cx(i, i+1)
            elif entanglement_gate.lower() == 'cz':
                qc.cz(i, i+1)
            elif entanglement_gate.lower() == 'rzx':
                qc.rzx(np.pi/4, i, i+1)
            else:
                raise Exception("Unknown entanglement gate: " + entanglement_gate)
    elif entanglement_topology == 'circular':
        for i in range(num_qubits - 1):
            if entanglement_gate.lower() == 'cx':
                qc.cx(i, i+1)
            elif entanglement_gate.lower() == 'cz':
                qc.cz(i, i+1)
            elif entanglement_gate.lower() == 'rzx':
                qc.rzx(np.pi/4, i, i+1)
            else:
                raise Exception("Unknown entanglement gate: " + entanglement_gate)
        if entanglement_gate.lower() == 'cx':
            qc.cx(num_qubits-1, 0)
        elif entanglement_gate.lower() == 'cz':
            qc.cz(num_qubits-1, 0)
        elif entanglement_gate.lower() == 'rzx':
            qc.rzx(np.pi/4, num_qubits-1, 0)
        else:
            raise Exception("Unknown entanglement gate: " + entanglement_gate)

def create_qte_circuit(bottleneck_size, num_qubits, num_blocks, entanglement_topology, entanglement_gate):
    """
    Build a parameterized QTE transformation (encoder) using multiple layers.

    For each block, we perform:
      - A layer of single-qubit rotations (we use Ry for simplicity).
      - An entangling layer whose connectivity is determined by entanglement_topology.

    The total number of parameters is:
      num_blocks * (2 * num_qubits)
    (Each layer has 2 * num_qubits parameters: one set before the entangling layer and one set after.)

    The decoder is taken as the inverse of the entire encoder.
    Returns qte_circuit, encoder, decoder_circuit, all_parameters
    """
    encoder = QuantumCircuit(num_qubits)
    params = []
    for layer in range(num_blocks):
        add_entanglement_topology(encoder, num_qubits, entanglement_topology, entanglement_gate)
        # add set of single qubit rotations
        for i in range(num_qubits):
            p = Parameter('Encoder Layer ' + str(layer) + ' Ry θ ' + str(i))
            params.append(p)
            encoder.ry(p, i)

    # For a proper autoencoder, you want to compress information into a bottleneck but
    # choosing which qubits to force to |0> reduces the flexibility the AE has in
    # compressing and reconstructing the data. Instead, in this design, the circuit
    # structure (and subsequent training loss) is expected to force the encoder to
    # focus its information into 'bottleneck_size' qubits.
    decoder = encoder.inverse()
    decoder_params = {
        p: Parameter(p.name.replace("Encoder", "Decoder")) for p in decoder.parameters
    }
    decoder.assign_parameters(decoder_params)
    params.extend(decoder.parameters)
    return encoder.compose(decoder), encoder, decoder, params

def create_full_circuit(num_qubits, config):
    """
    config is dict w/ additional hyperparameters:
        use_rx \
        use_ry  >                independent booleans signifying which rotation gates
        use_rz /                     to include for quantum embedding of the data
        num_blocks:              # [entanglement layer, rotation layer] repetitions in QTE
        entanglement_topology:   options are ['full', 'linear', 'circular']
        entanglement_gate:  options are ['CX', 'CZ', 'RZX']
        embedding_gate:          options are ['RX', 'RY', 'RZ']
        penalty_weight:          weight for the bottleneck penalty term.
    Returns full_circuit, encoder_circuit, decoder_circuit, input_params, trainable_params
    """
    embedding_gate = config.get('embedding_gate', 'rx')
    embedding_qc, input_params = create_embedding_circuit(num_qubits, embedding_gate)

    num_blocks = config.get('num_blocks', 1)
    ent_topology = config.get('entanglement_topology', 'full')
    ent_gate_type = config.get('entanglement_gate', 'cx')
    bottleneck_size = config.get('bottleneck_size', int(num_qubits/2))
    # create separate QC for QTE so that the decoder doesn't include the embedding method
    # when adding the encoder's inverse
    qte_circuit, encoder, decoder, trainable_params = create_qte_circuit(
        bottleneck_size, num_qubits, num_blocks, ent_topology, ent_gate_type
    )
    return embedding_qc.compose(qte_circuit), embedding_qc, encoder, decoder, input_params, trainable_params

def trash_qubit_penalty(state, bottleneck_size):
    """
    Compute a penalty based on the marginal probability of each qubit being in |0>.

    For each qubit in the state, compute P(|0>), then sort these probabilities in ascending order.
    Treat the lowest (num_qubits - bottleneck_size) probabilities as the trash qubits and define
    the penalty as the sum over these of (1 - P(|0>)).

    Parameters:
        state (Statevector): state from which to compute marginals
        bottleneck_size (int): # qubits reserved for latent space

    Returns:
        penalty (float): cumulative penalty for the trash qubits
    """
    marginals = []

    # compute marginal probability for each qubit being in |0>
    for q in range(state.num_qubits):
        trace_indices = list(range(state.num_qubits))
        trace_indices.remove(q)
        density_matrix = partial_trace(state, trace_indices)
        # probability that qubit q = |0> is (0, 0) entry of reduced density matrix
        pq = np.real(density_matrix.data[0, 0])
        marginals.append(pq)

    sorted_marginals = sorted(marginals)
    num_trash = state.num_qubits - bottleneck_size

    # marginals are probabilities that qubit is in |0>
    penalty = sum(1 - p for p in sorted_marginals[:num_trash])
    return penalty

def qae_cost_function(data, embedder, encoder, decoder, input_params, bottleneck_size, trash_qubit_penalty_weight=2, no_noise_prob=1.0):
    """
    With probability (1-no_noise_prob), the input state is perturbed by adding (or subtracting) the error vector
    from the previous time step. The perturbed state is both the input to the encoder and the reconstruction target.

    (1 - fidelity(current_state, reconstructed_state)) + trash_qubit_penalty_weight * trash_qubit_penalty()
    """
    total_cost = 0.0
    series_index = 0
    for series in data:
        print('        Calculating loss for series ' + str(series_index + 1))
        series_cost = 0.0
        previous_error_vector = None
        for state in series:
            params = {p: state[i] for i, p in enumerate(input_params)}
            ideal_qc = embedder.assign_parameters(params)
            ideal_state = Statevector.from_instruction(ideal_qc)
            ideal_array = ideal_state.data

            if previous_error_vector is not None and np.random.rand() < (1-no_noise_prob):
                sign = 1 if np.random.rand() < 0.5 else -1
                perturbed_array = ideal_array + sign * previous_error_vector
                # renormalize the perturbed vector to ensure valid quantum state
                perturbed_array = perturbed_array / np.linalg.norm(perturbed_array)
                input_state = Statevector(perturbed_array)
            else:
                input_state = ideal_state

            # pass the (possibly perturbed) input state through the encoder and decoder
            bottleneck_state = input_state.evolve(encoder)
            reconstructed_state = bottleneck_state.evolve(decoder)

            reconstruction_cost = 1 - state_fidelity(input_state, reconstructed_state)
            trash_penalty = trash_qubit_penalty_weight * trash_qubit_penalty(bottleneck_state, bottleneck_size)
            series_cost += reconstruction_cost + trash_penalty

            previous_error_vector = target_state.data - reconstructed_state.data

        total_cost += series_cost / len(series) # avg cost per state (always same num series)
        series_index += 1

    return total_cost

def qte_cost_function(data, embedder, encoder, decoder, input_params, bottleneck_size, trash_qubit_penalty_weight=2, teacher_forcing_prob=1.0):
    """
    Scheduled sampling is incorporated via teacher_forcing_prob:
      - With probability teacher_forcing_prob, the next input state is taken from the ground truth.
      - Otherwise, the model's predicted state is used as the next input.

    (1 - fidelity(next_state, predicted_state)) + trash_qubit_penalty_weight * trash_qubit_penalty()
    """
    total_cost = 0.0
    series_index = 0
    for series in data:
        print('        Calculating loss for series ' + str(series_index + 1))
        series_length = len(series)
        if series_length < 2:
            continue # can't model transition
        series_cost = 0.0

        # always use ground truth for first time step
        initial_params = {p: series[0][i] for i, p in enumerate(input_params)}
        qc_initial = embedder.assign_parameters(initial_params)
        current_state = Statevector.from_instruction(qc_initial)

        for i in range(series_length - 1):
            next_input_params = {p: series[i+1] for i, p in enumerate(input_params)}
            next_state = Statevector.from_instruction(embedder.assign_parameters(next_input_params))

            bottleneck_state = current_state.evolve(encoder)
            predicted_state = bottleneck_state.evolve(decoder)

            reconstruction_cost = 1 - state_fidelity(next_state, predicted_state)
            trash_penalty = trash_qubit_penalty_weight * trash_qubit_penalty(bottleneck_state, bottleneck_size)
            series_cost += reconstruction_cost + trash_penalty

            if np.random.rand() < teacher_forcing_prob:
                current_state = next_state
            else:
                current_state = predicted_state

        total_cost += series_cost / (series_length-1) # avg cost per transition (always same num series)
    return total_cost

def adam_update(params, gradients, moment1, moment2, t, lr, beta1=0.9, beta2=0.999, epsilon=1e-8):
    moment1 = beta1 * moment1 + (1 - beta1) * gradients
    moment2 = beta2 * moment2 + (1 - beta2) * (gradients ** 2)
    bias_corrected_moment1 = moment1 / (1 - beta1 ** t)
    bias_corrected_moment2 = moment2 / (1 - beta2 ** t)
    new_params = params - lr * bias_corrected_moment1 / (np.sqrt(bias_corrected_moment2) + epsilon)
    return new_params, moment1, moment2

def train_adam(training_data, validation_data, cost_function, config, num_epochs=100):
    """
    Train the QTE by minimizing the cost function using ADAM. Note that the QTE will only enforce
    the bottleneck via the cost function. This is done in order to balance efficiency w/ added
    flexibility for which qubits get thrown away.

    Parameters:
      - cost_function: the function to use for calculating the cost

      - config: dict containing additional hyperparameters:
            use_rx \
            use_ry  >                independent booleans signifying which rotation gates
            use_rz /                     to include for quantum embedding of the data
            num_blocks:              # [entanglement layer, rotation layer] repetitions per 1/2 of QTE
            entanglement_topology:   for all entanglement layers
            entanglement_gate:  options are ['CX', 'CZ', 'RZX']
            embedding_gate:          options are ['RX', 'RY', 'RZ']
            learning_rate:
            bottleneck_size:         number of qubits for the latent space
            penalty_weight:          weight for the bottleneck penalty term
      - num_epochs: number of training iterations

    Returns trained_circuit, cost_history
    """
    num_qubits = len(training_data[0][0])
    num_params = 4 * num_qubits * config['num_blocks'] # 2 layers per block, num_blocks is per encoder & decoder
    param_values = np.random.uniform(0., np.pi, size=num_params)
    cost_history = []
    moment1 = np.zeros_like(param_values)
    moment2 = np.zeros_like(param_values)
    gradient_width = 1e-4

    bottleneck_size = int(config['bottleneck_size'])
    num_blocks = int(config['num_blocks'])
    learning_rate = float(config['learning_rate'])
    penalty_weight = float(config.get('penalty_weight', 1.0))
    embedding_gate = config.get('embedding_gate', 'rx')

    trained_circuit, embedder, encoder, decoder, input_params, trainable_params = create_full_circuit(num_qubits, config)

    print('  created untrained circuit')

    previous_param_values = param_values.copy()
    for t in range(1, num_epochs + 1):
        print('  Epoch ' + str(t))
        param_dict = {param: value for param, value in zip(trainable_params, param_values)}
        encoder_bound = encoder.assign_parameters(param_dict)
        decoder_bound = decoder.assign_parameters(param_dict)

        print('    calculating initial cost')
        current_cost = cost_function(
            training_data, embedder, encoder_bound, decoder_bound, input_params, bottleneck_size, penalty_weight
        )
        cost_history.append(current_cost)
        print('      ', current_cost)

        gradients = np.zeros_like(param_values)
        # progressively increase the probability that the model will have to deal with it's own noise from the previous time step
        prob_own_noise = (t-1)/num_epochs
        for j in range(len(param_values)):
            print('    calculating gradient for param ' + str(j+1) + ' / ' + str(len(param_values)) + ' = ' + str((j)/len(param_values)*100) + '% done')
            params_eps = param_values.copy()
            params_eps[j] += gradient_width
            param_dict = {param: value for param, value in zip(trainable_params, params_eps)}
            encoder_bound = encoder.assign_parameters(param_dict)
            decoder_bound = decoder.assign_parameters(param_dict)
            gradients[j] = (cost_function(
                training_data, embedder, encoder_bound, decoder_bound, input_params, bottleneck_size, penalty_weight, prob_own_noise
            ) - current_cost) / gradient_width

        previous_param_values = param_values.copy()
        param_values, moment1, moment2 = adam_update(param_values, gradients, moment1, moment2, t, learning_rate)
        print('    Mean param update: ' + str(np.mean(param_values-previous_param_values)))
        print(f'    cost = {current_cost:.6f}')

    print('  calculating validation cost')
    validation_cost = cost_function(
        validation_data, embedder, encoder_bound, decoder_bound, input_params, bottleneck_size, penalty_weight
    )

    param_dict = {param: value for param, value in zip(trainable_params, param_values)}
    trained_circuit.assign_parameters(param_dict)
    return trained_circuit, cost_history, validation_cost

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(
        description="Train a Quantum Transition Encoder/Decoder and a Quantum Auto Encoder over the given data."
    )
    parser.add_argument("data_directory", type=str, help="Path to the directory containing the generated data.")
    args = parser.parse_args()

    from data_importers import import_generated
    dataset_partitions = import_generated(args.data_directory)

    d_i = 1
    for (training, validation) in dataset_partitions:
        num_qubits = len(training[0][0])
        config = {
            'bottleneck_size': num_qubits / 2,
            'num_blocks': 1,
            'learning_rate': .1,
            'penalty_weight': .75,
            'entanglement_topology': 'full',
            'entanglement_gate': 'rzx',
            'embedding_gate': 'rz',
        }
        for type in ['qae', 'qte']:
            print('Training ' + type.upper() + ' for dataset ' + str(d_i))
            if type == 'qae':
                trained_circuit, cost_history, validation_cost = train_adam(training, validation, qae_cost_function, config, num_epochs=10)
            elif type == 'qte':
                trained_circuit, cost_history, validation_cost  = train_adam(training, validation, qte_cost_function, config, num_epochs=10)
            else:
                raise Exception('Unknown type: ' + args.type)

            print('  Final training cost:', cost_history[-1])
            print('  Validation cost: ', validation_cost)

            from qiskit import qpy
            import os
            with open(os.path.join(args.data_directory, f'dataset{d_i}_' + type + '_trained_circuit.qpy'), 'wb') as file:
                qpy.dump(trained_circuit, file)
