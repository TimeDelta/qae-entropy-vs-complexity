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
    print('    calculating trash qubit penalty')

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

def qae_cost_function(data, embedder, encoder, decoder, input_params, bottleneck_size, trash_qubit_penalty_weight=2):
    """
    (1 - fidelity(ideal_state, reconstructed_state)) + trash_qubit_penalty_weight * trash_qubit_penalty().
    """
    total_cost = 0.0
    num_qubits = len(data[0])
    for sample in data:
        sample_cost = 0
        for state in sample:
            print('Emedding current state')
            params = {
                # each input param ends in an index
                p: state[int(re.search(r'\d+$', p.name).group())]for p in input_params
            }
            ideal_qc = embedder.assign_parameters(params)
            ideal_state = Statevector.from_instruction(ideal_qc)

            print('retrieving bottleneck state')
            bottleneck_state = ideal_state.evolve(encoder)
            reconstructed_state = bottleneck_state.evolve(decoder)

            print('    calculating fidelity')
            reconstruction_cost = 1 - state_fidelity(ideal_state, reconstructed_state)
            trash_penalty = trash_qubit_penalty_weight * trash_qubit_penalty(bottleneck_state, bottleneck_size)
            sample_cost += reconstruction_cost + trash_qubit_penalty_weight * trash_penalty
        total_cost += sample_cost / sample_length # avg cost per state
    return total_cost

def qte_cost_function(data, embedder, encoder, decoder, input_params, bottleneck_size, trash_qubit_penalty_weight=2):
    """
    (1 - fidelity(ideal_state, reconstructed_state)) + trash_qubit_penalty_weight * trash_qubit_penalty().
    """
    total_cost = 0.0
    num_qubits = len(data[0][0])
    for sample in data:
        sample_length = len(sample)
        if sample_length < 2:
            continue # can't model transition
        sample_cost = 0
        for i in range(sample_length - 1):
            current_state = sample[i]
            next_state = sample[i+1]

            # each input param name ends in an index
            current_input_params = {p: current_state[i] for i, p in enumerate(input_params)}
            next_input_params = {p: next_state[i] for i, p in enumerate(input_params)}

            input_qc = embedder.assign_parameters(current_input_params)
            input_state = Statevector.from_instruction(input_qc)
            ideal_qc = embedder.assign_parameters(next_input_params)
            ideal_state = Statevector.from_instruction(ideal_qc)

            bottleneck_state = input_state.evolve(encoder)
            predicted_state = bottleneck_state.evolve(decoder)

            reconstruction_cost = 1 - state_fidelity(ideal_state, reconstructed_state)
            trash_penalty = trash_qubit_penalty_weight * trash_qubit_penalty(bottleneck_state, bottleneck_size)
            sample_cost += reconstruction_cost + trash_qubit_penalty_weight * trash_penalty
        total_cost += sample_cost / (sample_length-1) # avg cost per transition
    return total_cost

def adam_update(params, gradients, moment1, moment2, t, lr, beta1=0.9, beta2=0.999, epsilon=1e-8):
    moment1 = beta1 * moment1 + (1 - beta1) * gradients
    moment2 = beta2 * moment2 + (1 - beta2) * (gradients ** 2)
    bias_corrected_moment1 = moment1 / (1 - beta1 ** t)
    bias_corrected_moment2 = moment2 / (1 - beta2 ** t)
    new_params = params - lr * bias_corrected_moment1 / (np.sqrt(bias_corrected_moment2) + epsilon)
    return new_params, moment1, moment2

def train_adam(data, cost_function, config, num_epochs=100):
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
    num_qubits = len(data[0])
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
            data, embedder, encoder_bound, decoder_bound, input_params, bottleneck_size, penalty_weight
        )
        cost_history.append(current_cost)

        gradients = np.zeros_like(param_values)
        for j in range(len(param_values)):
            params_eps = param_values.copy()
            params_eps[j] += gradient_width
            param_dict = {param: value for param, value in zip(trainable_params, params_eps)}
            encoder_bound = encoder.assign_parameters(param_dict)
            decoder_bound = decoder.assign_parameters(param_dict)
            gradients[j] = (cost_function(
                data, embedder, encoder_bound, decoder_bound, input_params, bottleneck_size, penalty_weight
            ) - current_cost) / gradient_width

        previous_param_values = param_values.copy()
        param_values, moment1, moment2 = adam_update(param_values, gradients, moment1, moment2, t, learning_rate)
        print('Mean param update: ' + str(np.mean(param_values-previous_param_values)))

        print(f"Iteration {t}: cost = {current_cost:.6f}")

    param_dict = {param: value for param, value in zip(trainable_params, param_values)}
    trained_circuit.assign_parameters(param_dict)
    return trained_circuit, cost_history

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(
        description="Train a Quantum Transition Encoder/Decoder or a Quantum Auto Encoder over the given data."
    )
    parser.add_argument("data_directory", type=str, help="Path to the directory containing the training data.")
    parser.add_argument("--bottleneck_size", type=int, default=0)
    parser.add_argument("--num_blocks", type=int, default=0)
    parser.add_argument("--learning_rate", type=int, default=0)
    parser.add_argument("--penalty_weight", type=int, default=0)
    parser.add_argument("--sample_length", type=int, default=0)
    parser.add_argument("--dataset", type=str, default='single_subject', help="Options: 'FACED', 'single_subject'")
    parser.add_argument("--type", type=str, default='qae', help="QAE or QTE (case-insensitive)")
    args = parser.parse_args()
    # input_data = np.array([[[.5, 1., 1.5, 2.],[.8, .8, 1.3, 2.]], [[1,1,1,1],[2,3,1,4]]])

    if args.dataset.upper() == 'FACED':
        from data_importers import import_FACED
        input_data = import_FACED(args.data_directory)
    elif args.dataset.lower() == 'single_subject':
        from data_importers import import_single_subject
        input_data = import_single_subject(args.data_directory)
    else:
        raise Exception('Unknown dataset type: ' + args.dataset)

    num_qubits = len(input_data[0])
    bottleneck_size = args.bottleneck_size
    if bottleneck_size == 0:
        bottleneck_size = np.random.randint(1, num_qubits)
    penalty_weight = args.penalty_weight
    if penalty_weight == 0:
        penalty_weight = 10 ** np.random.uniform(-4, 0)
    num_blocks = args.num_blocks
    if num_blocks == 0:
        num_blocks = np.random.randint(1, 10)
    learning_rate = args.learning_rate
    if learning_rate == 0:
        learning_rate = 10 ** np.random.uniform(-4, -1)
    config = {
        'bottleneck_size': bottleneck_size,
        'num_blocks': num_blocks,
        'learning_rate': learning_rate,
        'penalty_weight': penalty_weight,
        'entanglement_topology': np.random.choice(ENTANGLEMENT_OPTIONS),
        'entanglement_gate': np.random.choice(ENTANGLEMENT_GATES),
        'embedding_gate': np.random.choice(EMBEDDING_ROTATION_GATES),
    }
    print(config)

    if args.type.lower() == 'qae':
        trained_circuit, cost_history = train_adam(input_data, qte_cost_function, config, num_epochs=10)
    elif args.type.lower() == 'qte':
        trained_circuit, cost_history = train_adam(input_data, qae_cost_function, config, num_epochs=10)
    else:
        raise Exception('Unknown type: ' + args.type)

    print("\nTrained parameters:", trained_circuit.draw())
    print("Final reconstruction cost:", cost_history[-1])