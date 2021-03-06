import os
import numbers
import sympy
import scipy
import pandas as pd
import numpy as np
import qutip

import theano
import theano.tensor as T

import matplotlib.pyplot as plt
import seaborn as sns

from .utils import complex2bigreal
from .QubitNetwork import QubitNetwork


def _gradient_updates_momentum(params, grad, learning_rate, momentum):
    """
    Compute updates for gradient descent with momentum

    Parameters
    ----------
    cost : theano.tensor.var.TensorVariable
        Theano cost function to minimize
    params : list of theano.tensor.var.TensorVariable
        Parameters to compute gradient against
    learning_rate : float
        Gradient descent learning rate
    momentum : float
        Momentum parameter, should be at least 0 (standard gradient
        descent) and less than 1

    Returns
    -------
    updates : list
        List of updates, one for each parameter
    """
    # Make sure momentum is a sane value
    assert momentum < 1 and momentum >= 0
    # List of update steps for each parameter
    updates = []
    if not isinstance(params, list):
        params = [params]
    # Just gradient descent on cost
    for param in params:
        # For each parameter, we'll create a previous_step shared variable.
        # This variable keeps track of the parameter's update step
        # across iterations. We initialize it to 0
        previous_step = theano.shared(
            param.get_value() * 0., broadcastable=param.broadcastable)
        step = momentum * previous_step + learning_rate * grad
        # Add an update to store the previous step value
        updates.append((previous_step, step))
        # Add an update to apply the gradient descent step to the
        # parameter itself
        updates.append((param, param + step))
    return updates


def _gradient_updates_adadelta(params, grads):
    eps = 1e-6
    rho = 0.95
    # initialize needed shared variables
    def shared_from_var(p):
        return theano.shared(
            p.get_value() * np.asarray(0, dtype=theano.config.floatX))
    zgrads = shared_from_var(params)
    rparams2 = shared_from_var(params)
    rgrads2 = shared_from_var(params)

    zgrads_update = (zgrads, grads)
    rgrads2_update = (rgrads2, rho * rgrads2 + (1 - rho) * grads**2)

    params_step = -T.sqrt(rparams2 + eps) / T.sqrt(rgrads2 + eps) * zgrads
    rparams2_update = (rparams2, rho * rparams2 + (1 - rho) * params_step**2)
    params_update = (params, params - params_step)

    updates = (zgrads_update, rgrads2_update, rparams2_update, params_update)
    return updates


def _split_bigreal_ket(ket):
    """Splits in half a real vector of length 2N

    Given an input ket vector in big real form, returns a pair of real
    vectors, the first containing the first N elements, and the second
    containing the last N elements.
    """
    ket_real = ket[:ket.shape[0] // 2]
    ket_imag = ket[ket.shape[0] // 2:]
    return ket_real, ket_imag


def _ket_to_dm(ket):
    """Builds theano function to convert kets in dms in big real form.

    The input is expected to be a 1d real array, storing the state
    vector as `(psi_real, psi_imag)`, where `psi` is the complex vector.
    The outputs are real and imaginary part of the corresponding density
    matrix.
    """
    ket_real, ket_imag = _split_bigreal_ket(ket)
    ket_real = ket_real[:, None]
    ket_imag = ket_imag[:, None]

    dm_real = ket_real * ket_real.T + ket_imag * ket_imag.T
    dm_imag = ket_imag * ket_real.T - ket_real * ket_imag.T
    return dm_real, dm_imag


def _compute_fidelities_col_fn(col_idx, row_idx, matrix, num_ancillae):
    """
    `_compute_fidelities_col_fn` and `(...)row_fn` are the functions that
    handle the computation of the partial traces. The latter is run on
    each block of rows of the matrix to partial trace, with each block
     containing `2 ** num_ancillae` rows.
    For each block of rows, the former scans through the corresponding
    blocks of columns, taking the trace of each resulting submatrix.
    """
    subm_dim = 2**num_ancillae
    return T.nlinalg.trace(matrix[row_idx * subm_dim:(row_idx + 1) * subm_dim,
                                  col_idx * subm_dim:(col_idx + 1) * subm_dim])


def _compute_fidelities_row_fn(row_idx, matrix, num_ancillae):
    """See `_compute_fidelities_col_fn`."""
    results, _ = theano.scan(
        fn=_compute_fidelities_col_fn,
        sequences=T.arange(matrix.shape[1] // 2**num_ancillae),
        non_sequences=[row_idx, matrix, num_ancillae])
    return results



def _fidelity_with_ptrace(i, matrix, target_states, num_ancillae):
    """
    Compute fidelity between target and obtained states.

    This function is intended to be called in `theano.scan` from
    `QubitNetwork.fidelity`, and it operates with `theano` "symbolic"
    tensor objects. It *does not* operate with numbers.

    Parameters
    ----------
    i : int
        Denotes the element to take from `matrix`. This is necessary
        because of how `theano.scan` works (it is not possible to just
        pass the corresponding matrix[i] element to the function).
    matrix : theano 2d array
        An array of training states. `matrix[i]` is the `i`-th training
        state after evolution through `exp(-1j * H)`, *in big real ket
        form*.
        In other words, `matrix` is the set of states obtained after
        the evolution through the net, to be compared with the
        corresponding set of training target states.
        `matrix[i]` has length `2 * (2 ** num_qubits)`, with
    target_states : theano 2d array
        The set of target states. `target_states[i]` is the state that
        we would like `matrix[i]` to be equal to.
    num_ancillae : int
        The number of ancillae in the network.
    """
    # - `dm_real` and `dm_imag` will be square matrices of length
    #   `2 ** num_qubits`.
    dm_real, dm_imag = _ket_to_dm(matrix[i])
    # `dm_real_traced` and `dm_imag_traced` are square matrices
    # of length `2 ** num_system_qubits`.
    dm_real_traced, _ = theano.scan(
        fn=_compute_fidelities_row_fn,
        sequences=T.arange(dm_real.shape[0] // 2**num_ancillae),
        non_sequences=[dm_real, num_ancillae])
    dm_imag_traced, _ = theano.scan(
        fn=_compute_fidelities_row_fn,
        sequences=T.arange(dm_imag.shape[0] // 2**num_ancillae),
        non_sequences=[dm_imag, num_ancillae])

    #  ---- Old method to compute trace of product of dms: ----
    # target_dm_real, target_dm_imag = _ket_to_dm(target_states[i])

    # prod_real = (T.dot(dm_real_traced, target_dm_real) -
    #              T.dot(dm_imag_traced, target_dm_imag))
    # tr_real = T.nlinalg.trace(prod_real)

    # # we need only take the trace of the real part of the product,
    # # as if \rho and \rho' are two Hermitian matrices, then
    # # Tr(\rho_R \rho'_I) = Tr(\rho_I \rho'_R) = 0.
    # return tr_real

    # ---- New method: ----
    target_real, target_imag = _split_bigreal_ket(target_states[i])

    # `psi` and `psi_tilde` have length 2 * (2 ** numSystemQubits)
    psi = target_states[i][:, None]
    psi_tilde = T.concatenate((-target_imag, target_real))[:, None]
    # `big_dm` is a square matrix with same length
    big_dm = T.concatenate((
        T.concatenate((dm_imag_traced, dm_real_traced), axis=1),
        T.concatenate((-dm_real_traced, dm_imag_traced), axis=1)
    ), axis=0)
    out_fidelity = psi.T.dot(big_dm).dot(psi_tilde)
    return out_fidelity


def _fidelity_no_ptrace(i, states, target_states):
    """
    Compute symbolic fidelity between `states[i]` and `target_states[i]`.

    Both `states[i]` and `target_states[i]` are real vectors of same
    length.
    """
    state = states[i]
    target_state = target_states[i]

    # state_real = state[:state.shape[0] // 2]
    # state_imag = state[state.shape[0] // 2:]
    # target_state_real = target_state[:target_state.shape[0] // 2]
    # target_state_imag = target_state[target_state.shape[0] // 2:]
    state_real, state_imag = _split_bigreal_ket(state)
    target_state_real, target_state_imag = _split_bigreal_ket(target_state)

    fidelity_real = (T.dot(state_real, target_state_real) +
                     T.dot(state_imag, target_state_imag))
    fidelity_imag = (T.dot(state_real, target_state_imag) -
                     T.dot(state_imag, target_state_real))
    fidelity = fidelity_real ** 2 + fidelity_imag ** 2
    return fidelity


class TargetGateNotGivenError(Exception):
    pass


class QubitNetworkModel(QubitNetwork):
    """Handling of theano graph buliding on top of the QubitNetwork.

    Here we add the theano variables and functions to compute fidelity
    and so on.
    """
    def __init__(self, num_qubits=None, num_system_qubits=None,
                 interactions=None,
                 net_topology=None,
                 sympy_expr=None,
                 free_parameters_order=None,
                 ancillae_state=None,
                 initial_values=None,
                 target_gate=None):
        # Initialize `QubitNetwork` parent
        super().__init__(num_qubits=num_qubits,
                         num_system_qubits=num_system_qubits,
                         interactions=interactions,
                         ancillae_state=ancillae_state,
                         net_topology=net_topology,
                         sympy_expr=sympy_expr,
                         free_parameters_order=free_parameters_order)
        # attributes initialization
        self.initial_values = self._set_initial_values(initial_values)
        self.parameters, self.hamiltonian_model = self.build_theano_graph()
        # self.inputs and self.outputs are the holders for the training/testing
        # inputs and correpsonding output states. They are used to build
        # the theano expression for the `fidelity`.
        self.inputs = T.dmatrix('inputs')
        self.outputs = T.dmatrix('outputs')
        self.target_gate = target_gate

    @staticmethod
    def _fidelities_with_ptrace(output_states, target_states, num_ancillae):
        """Compute fidelities in the case of ancillary qubits.

        This function handles the case of the fidelity when the output
        states are *larger* than the target states. In this case the
        fidelity is computed taking the partial trace with respect to
        the ancillary degrees of freedom of the output, and taking the
        fidelity of the resulting density matrix with the target
        (pure) state.
        """
        num_states = output_states.shape[0]
        fidelities, _ = theano.scan(
            fn=_fidelity_with_ptrace,
            sequences=T.arange(num_states),
            non_sequences=[output_states, target_states, num_ancillae]
        )
        return fidelities

    @staticmethod
    def _fidelities_no_ptrace(output_states, target_states):
        """Compute fidelities when there are no ancillary qubits.
        """
        num_states = output_states.shape[0]
        fidelities, _ = theano.scan(
            fn=_fidelity_no_ptrace,
            sequences=T.arange(num_states),
            non_sequences=[output_states, target_states]
        )
        return fidelities

    def compute_evolution_matrix(self):
        """Compute matrix exponential of iH."""
        return T.slinalg.expm(self.hamiltonian_model)

    def _target_outputs_from_inputs_open_map(self, input_states):
        raise NotImplementedError('Not implemented yet')
        # Note that in case of an open map target, all target states are
        # density matrices, instead of just kets like they would when the
        # target is a unitary gate.
        target_states = []
        for psi in input_states:
            # the open evolution is implemented vectorizing density
            # matrices and maps: `A * rho * B` becomes
            # `unvec(vec(tensor(A, B.T)) * vec(rho))`.
            vec_dm_ket = qutip.operator_to_vector(qutip.ket2dm(psi))
            evolved_ket = self.target_gate * vec_dm_ket
            evolved_ket = qutip.vector_to_operator(evolved_ket)
            target_states.append(evolved_ket)
        return target_states

    def _target_outputs_from_inputs(self, input_states):
        # defer operation to other method for open maps
        if self.target_gate.issuper:
            return self._target_outputs_from_inputs_open_map(input_states)
        # unitary evolution of input states. `target_gate` is qutip obj
        return [self.target_gate * psi for psi in input_states]

    def generate_training_states(self, num_states):
        """Create training states for the training.

        This function generates every time it is called a set of
        input and corresponding target output states, to be used during
        training. These values will be used during the computation
        through the `givens` parameter of `theano.function`.

        Returns
        -------
        A tuple with two elements: training vectors and labels.
        NOTE: The training and target vectors have different lengths!
              The latter span the whole space while the former only the
              system one.

        training_states: an array of vectors.
            Each vector represents a state in the full system+ancilla space,
            in big real form. These states span the whole space simply
            out of convenience, but are obtained as tensor product of
            the target states over the system qubits with the initial
            states of the ancillary qubits.
        target_states: an array of vectors.
            Each vector represents a state spanning only the system qubits,
            in big real form. Every such state is generated by evolving
            the corresponding `training_state` through the matrix
            `target_unitary`.

        This generation method is highly non-optimal. However, it takes
        about ~250ms to generate a (standard) training set of 100 states,
        which amounts to ~5 minutes over 1000 epochs with a training dataset
        size of 100, making this factor not particularly important.
        """
        assert self.target_gate is not None, 'target_gate not set'

        # 1) Generate random input states over system qubits
        # `rand_ket_haar` seems to be slightly faster than `rand_ket`
        length_inputs = 2 ** self.num_system_qubits
        qutip_dims = [[2 for _ in range(self.num_system_qubits)],
                      [1 for _ in range(self.num_system_qubits)]]
        training_inputs = [
            qutip.rand_ket_haar(length_inputs, dims=qutip_dims)
            for _ in range(num_states)
        ]
        # 2) Compute corresponding output states
        target_outputs = self._target_outputs_from_inputs(training_inputs)
        # 3) Tensor product of training input states with ancillae
        for idx, ket in enumerate(training_inputs):
            if self.num_system_qubits < self.num_qubits:
                ket = qutip.tensor(ket, self.ancillae_state)
            training_inputs[idx] = complex2bigreal(ket)
        training_inputs = np.asarray(training_inputs)
        # 4) Convert target outputs in big real form.
        # NOTE: the target states are kets if the target gate is unitary,
        #       and density matrices for target open maps.
        target_outputs = np.asarray(
            [complex2bigreal(st) for st in target_outputs])
        # return results as matrices
        _, len_inputs, _ = training_inputs.shape
        _, len_outputs, _ = target_outputs.shape
        training_inputs = training_inputs.reshape((num_states, len_inputs))
        target_outputs = target_outputs.reshape((num_states, len_outputs))
        return training_inputs, target_outputs

    def fidelity_test(self, n_samples=10, return_mean=True):
        """Compute fidelity with current interaction values with qutip.

        This can be used to compute the fidelity avoiding the
        compilation of the theano graph done by `self.fidelity`.

        Raises
        ------
        TargetGateNotGivenError if not target gate has been specified.
        """
        # compute fidelity for case of no ancillae
        if self.target_gate is None:
            raise TargetGateNotGivenError('You must give a target gate'
                                          ' first.')
        target_gate = self.target_gate
        gate = qutip.Qobj(self.get_current_gate(),
                          dims=[[2] * self.num_qubits] * 2)
        # each element of `fidelities` will contain the fidelity obtained with
        # a single randomly generated input state
        fidelities = np.zeros(n_samples)
        for idx in range(fidelities.shape[0]):
            # generate random input state (over system qubits only)
            psi_in = qutip.rand_ket_haar(2 ** self.num_system_qubits)
            psi_in.dims = [
                [2] * self.num_system_qubits, [1] * self.num_system_qubits]
            # embed it into the bigger system+ancilla space (if necessary)
            if self.num_system_qubits < self.num_qubits:
                Psi_in = qutip.tensor(psi_in, self.ancillae_state)
            else:
                Psi_in = psi_in
            # evolve input state
            Psi_out = gate * Psi_in
            # trace out ancilla (if there is an ancilla to trace)
            if self.num_system_qubits < self.num_qubits:
                dm_out = Psi_out.ptrace(range(self.num_system_qubits))
            else:
                dm_out = qutip.ket2dm(Psi_out)
            # compute fidelity
            fidelity = (psi_in.dag() * target_gate.dag() *
                        dm_out * target_gate * psi_in)
            fidelities[idx] = fidelity[0, 0].real
        if return_mean:
            return fidelities.mean()
        else:
            return fidelities

    def fidelity(self, return_mean=True):
        """Return theano graph for fidelity given training states.

        In the output theano expression `fidelities`, the tensors
        `output_states` and `target_states` are left "hanging", and will
        be replaced during the training through the `givens` parameter
        of `theano.function`.
        """
        output_states = T.tensordot(
            self.compute_evolution_matrix(), self.inputs, axes=([1], [1])).T
        num_ancillae = self.num_qubits - self.num_system_qubits
        if num_ancillae > 0:
            fidelities = self._fidelities_with_ptrace(
                output_states, self.outputs, num_ancillae)
        else:
            fidelities = self._fidelities_no_ptrace(output_states,
                                                    self.outputs)
        if return_mean:
            return T.mean(fidelities)
        else:
            return fidelities

    def _set_initial_values(self, values=None):
        """Set initial values for the parameters in the Hamiltonian.

        If no explicit values are given, the parameters are initialized
        with zeros. The computed initial values are returned, to be
        stored in self.initial_values from __init__
        """
        if values is None:
            initial_values = np.random.randn(len(self.free_parameters))
        elif isinstance(values, numbers.Number):
            initial_values = np.ones(len(self.free_parameters)) * values
        # A dictionary can be used to directly set the values of some of
        # the parameters. Each key of the dictionary can be either a
        # 1) sympy symbol correponding to an interaction, 2) a string
        # with the same name of a symbol of an interaction or 3) a tuple
        # of integers corresponding to a given interactions. This last
        # option is not valid if the Hamiltonian was created using a
        # sympy expression.
        # All the symbols not specified in the dictionary are initialized
        # to zero.
        elif isinstance(values, dict):
            init_values = np.zeros(len(self.free_parameters))
            symbols_dict = dict(zip(
                self.free_parameters, range(len(self.free_parameters))))
            for symb, value in values.items():
                # if `symb` is a single number, make a 1-element tuple
                if isinstance(symb, numbers.Number):
                    symb = (symb,)
                # convert strings to corresponding sympy symbols
                if isinstance(symb, str):
                    symb = sympy.Symbol(symb)
                # `symb` can be a tuple when a key is of the form
                # `(1, 3)` to indicate an X1Z2 interaction.
                elif isinstance(symb, tuple):
                    symb = 'J' + ''.join(str(char) for char in symb)
                try:
                    init_values[symbols_dict[symb]] = value
                except KeyError:
                    raise ValueError('The symbol {} doesn\'t match'
                                     ' any of the names of parameters of '
                                     'the model.'.format(str(symb)))
            initial_values = init_values
        else:
            initial_values = values

        return initial_values

    def _get_bigreal_matrices(self, multiply_by_j=True):
        """
        Multiply each element of `self.matrices` with `-1j`, and return
        them converted to big real form. Or optionally do not multiply
        with the imaginary unit and just return the matrix coefficients
        converted in big real form.
        """
        if multiply_by_j:
            return [complex2bigreal(-1j * matrix).astype(np.float)
                    for matrix in self.matrices]
        else:
            return [complex2bigreal(matrix).astype(np.float)
                    for matrix in self.matrices]

    def build_theano_graph(self):
        """Build theano object corresponding to the Hamiltonian model.

        The free parameters in the output graphs are taken from the sympy
        free symbols in the Hamiltonian, stored in `self.free_parameters`.

        Returns
        -------
        tuple with the shared theano variable representing the parameters
        and the corresponding theano.tensor object for the Hamiltonian
        model, ***multiplied by -1j***.
        """
        # define the theano variables
        parameters = theano.shared(
            value=np.zeros(len(self.free_parameters), dtype=np.float),
            name='J',
            borrow=True  # still not sure what this does
        )
        parameters.set_value(self.initial_values)
        # multiply variables with matrix coefficients
        bigreal_matrices = self._get_bigreal_matrices()
        theano_graph = T.tensordot(parameters, bigreal_matrices, axes=1)
        # from IPython.core.debugger import set_trace; set_trace()
        return [parameters, theano_graph]

    def get_current_hamiltonian(self):
        """Return Hamiltonian of the system with current parameters.

        The returned Hamiltonian is a numpy.ndarray object.
        """
        ints_values = self.parameters.get_value()
        matrices = [np.asarray(matrix).astype(np.complex)
                    for matrix in self.matrices]
        final_matrix = np.zeros_like(matrices[0])
        for matrix, parameter in zip(matrices, ints_values):
            final_matrix += parameter * matrix
        return final_matrix

    def get_current_gate(self, return_qobj=True):
        """Return the gate implemented by current interaction values."""
        gate = scipy.linalg.expm(-1j * self.get_current_hamiltonian())
        if return_qobj:
            return qutip.Qobj(gate, dims=[[2] * self.num_qubits] * 2)
        return gate


class Optimizer:
    """
    Main object handling the optimization of a `QubitNetwork` instance.
    """
    # pylint: disable=too-many-instance-attributes
    def __init__(self, net,
                 learning_rate=None, decay_rate=None,
                 training_dataset_size=10,
                 test_dataset_size=10,
                 batch_size=None,
                 n_epochs=None,
                 target_gate=None,
                 sgd_method='momentum'):
        # the net parameter can be a QubitNetwork object or a str
        self.net = Optimizer._load_net(net)
        self.net.target_gate = target_gate
        self.hyperpars = dict(
            train_dataset_size=training_dataset_size,
            test_dataset_size=test_dataset_size,
            batch_size=batch_size,
            n_epochs=n_epochs,
            sgd_method=sgd_method,
            initial_learning_rate=learning_rate,
            decay_rate=decay_rate
        )
        # self.vars stores the shared variables for the computation
        def _sharedfloat(arr, name):
            return theano.shared(np.asarray(
                arr, dtype=theano.config.floatX), name=name)
        inputs_length = 2 * 2**self.net.num_qubits
        outputs_length = 2 * 2**self.net.num_system_qubits
        self.vars = dict(
            index=T.lscalar('minibatch index'),
            learning_rate=_sharedfloat(learning_rate, 'learning rate'),
            train_inputs=_sharedfloat(
                np.zeros((training_dataset_size, inputs_length)),
                'training inputs'),
            train_outputs=_sharedfloat(
                np.zeros((training_dataset_size, outputs_length)),
                'training outputs'),
            test_inputs=_sharedfloat(
                np.zeros((test_dataset_size, inputs_length)),
                'test inputs'),
            test_outputs=_sharedfloat(
                np.zeros((test_dataset_size, outputs_length)),
                'test outputs'),
            parameters=self.net.parameters
        )
        self.cost = self.net.fidelity()
        self.cost.name = 'mean fidelity'
        self.grad = T.grad(cost=self.cost, wrt=self.vars['parameters'])
        self.train_model = None  # to be assigned in `compile_model`
        self.test_model = None  # assigned in `compile_model`
        # define updates, to be performed at every call of `train_XXX`
        self.updates = self._make_updates(sgd_method)
        # initialize log to be filled with the history later
        self.log = {'fidelities': None, 'parameters': None}
        # create figure object
        self._fig = None
        self._ax = None

    @classmethod
    def load(cls, file):
        """Load from saved file."""
        import pickle
        _, ext = os.path.splitext(file)
        if ext != '.pickle':
            raise NotImplementedError('Only pickle files for now!')
        with open(file, 'rb') as f:
            data = pickle.load(f)
        net_data = data['net_data']
        opt_data = data['optimization_data']
        # create QubitNetwork instance
        num_qubits = np.log2(net_data['sympy_model'].shape[0]).astype(int)
        if net_data['ancillae_state'] is None:
            num_system_qubits = num_qubits
        else:
            raise NotImplementedError('WIP')
        net = QubitNetworkModel(
            num_qubits=num_qubits,
            num_system_qubits=num_system_qubits,
            free_parameters_order=net_data['free_parameters'],
            sympy_expr=net_data['sympy_model'],
            initial_values=opt_data['final_interactions'])
        # call __init__ to create `Optimizer` instance
        hyperpars = opt_data['hyperparameters']
        optimizer = cls(
            net,
            learning_rate=hyperpars['initial_learning_rate'],
            decay_rate=hyperpars['decay_rate'],
            training_dataset_size=hyperpars['train_dataset_size'],
            test_dataset_size=hyperpars['test_dataset_size'],
            batch_size=hyperpars['batch_size'],
            n_epochs=hyperpars['n_epochs'],
            sgd_method=hyperpars['sgd_method'],
            target_gate=opt_data['target_gate'])
        optimizer.log = opt_data['log']
        return optimizer

    @staticmethod
    def _load_net(net):
        """
        Parse the `net` parameter given during init of `Optimizer`.
        """
        if isinstance(net, str):
            raise NotImplementedError('To be reimplemented')
        return net

    def _get_meaningful_history(self):
        fids = self.log['fidelities']
        # we cut from the history the last contiguous block of
        # values that are closer to 1 than `eps`
        eps = 1e-10
        try:
            end_useful_log = np.diff(np.abs(1 - fids) < eps).nonzero()[0][-1]
        # if the fidelity didn't converge to 1 the above raises an
        # IndexError. We then look to remove all the trailing zeros
        except IndexError:
            try:
                end_useful_log = np.diff(fids == 0).nonzero()[0][-1]
            # if also the above doesn't work, we just return the whole thing
            except IndexError:
                end_useful_log = len(fids)
        saved_log = dict()
        saved_log['fidelities'] = fids[:end_useful_log]
        if self.log['parameters'] is not None:
            saved_log['parameters'] = self.log['parameters'][:end_useful_log]
        return saved_log

    def save_results(self, file):
        """Save optimization results.

        The idea is here to save all the information required to
        reproduce a given training session.
        """
        net_data = dict(
            sympy_model=self.net.get_matrix(),
            free_parameters=self.net.free_parameters,
            ancillae_state=self.net.ancillae_state
        )
        optimization_data = dict(
            target_gate=self.net.target_gate,
            hyperparameters=self.hyperpars,
            initial_interactions=self.net.initial_values,
            final_interactions=self._get_meaningful_history()['parameters'][-1]
        )
        # cut redundant log history
        optimization_data['log'] = self._get_meaningful_history()
        # prepare and finally save to file
        data_to_save = dict(
            net_data=net_data, optimization_data=optimization_data)
        _, ext = os.path.splitext(file)
        if ext == '.pickle':
            import pickle
            with open(file, 'wb') as fp:
                pickle.dump(data_to_save, fp)
            print('Successfully saved to {}'.format(file))
        else:
            raise ValueError('Only saving to pickle is supported.')


    def _make_updates(self, sgd_method):
        """Return updates, for `train_model` and `test_model`."""
        assert isinstance(sgd_method, str)
        # specify how to update the parameters of the model as a list of
        # (variable, update expression) pairs
        if sgd_method == 'momentum':
            momentum = 0.5
            learn_rate = self.vars['learning_rate']
            updates = _gradient_updates_momentum(
                self.vars['parameters'], self.grad,
                learn_rate, momentum)
        elif sgd_method == 'adadelta':
            updates = _gradient_updates_adadelta(
                self.vars['parameters'], self.grad)
        else:
            new_pars = self.vars['parameters']
            new_pars += self.vars['learning_rate'] * self.grad
            updates = [(self.vars['parameters'], new_pars)]
        return updates

    def _update_fig(self, len_shown_history):
        # retrieve or create figure object
        if self._fig is None:
            self._fig, self._ax = plt.subplots(1, 1, figsize=(10, 5))
        fig, ax = self._fig, self._ax
        ax.clear()
        # plot new fidelities
        n_epoch = self.log['n_epoch']
        fids = self.log['fidelities']
        if len_shown_history is None:
            ax.plot(fids[:n_epoch], '-b', linewidth=1)
        else:
            if n_epoch + 1 == len_shown_history:
                x_coords = np.arange(
                    n_epoch - len_shown_history + 1, n_epoch + 1)
            else:
                x_coords = np.arange(n_epoch + 1)
            ax.plot(x_coords, fids[x_coords], '-b', linewidth=1)
        plt.suptitle('learning rate: {}\nfidelity: {}'.format(
            self.vars['learning_rate'].get_value(), fids[n_epoch]))
        fig.canvas.draw()

    def refill_test_data(self):
        """Generate new test data and put them in shared variable.
        """
        inputs, outputs = self.net.generate_training_states(
            self.hyperpars['test_dataset_size'])
        self.vars['test_inputs'].set_value(inputs)
        self.vars['test_outputs'].set_value(outputs)

    def refill_training_data(self):
        """Generate new training data and put them in shared variable.
        """
        inputs, outputs = self.net.generate_training_states(
            self.hyperpars['train_dataset_size'])
        self.vars['train_inputs'].set_value(inputs)
        self.vars['train_outputs'].set_value(outputs)

    def train_epoch(self):
        """Generate training states and train for an epoch."""
        self.refill_training_data()
        n_train_batches = (self.hyperpars['train_dataset_size'] //
                           self.hyperpars['batch_size'])
        for minibatch_index in range(n_train_batches):
            self.train_model(minibatch_index)

    def test_epoch(self, save_parameters=True):
        """Compute fidelity, and store fidelity and parameters."""
        fidelity = self.test_model()
        n_epoch = self.log['n_epoch']
        if save_parameters:
            self.log['parameters'][n_epoch] = (
                self.vars['parameters'].get_value())
        self.log['fidelities'][n_epoch] = fidelity

    def _compile_model(self):
        """Compile train and test models.

        Compile the training function `train_model`, that while computing
        the cost at every iteration (batch), also updates the weights of
        the network based on the rules defined in `updates`.
        """
        batch_size = self.hyperpars['batch_size']
        batch_start = self.vars['index'] * batch_size
        batch_end = (self.vars['index'] + 1) * batch_size
        train_inputs_batch = self.vars['train_inputs'][batch_start: batch_end]
        train_outputs_batch = self.vars['train_outputs'][batch_start: batch_end]
        print('Compiling model ...', end='')
        self.train_model = theano.function(
            inputs=[self.vars['index']],
            outputs=self.cost,
            updates=self.updates,
            givens={
                self.net.inputs: train_inputs_batch,
                self.net.outputs: train_outputs_batch
            })
        # `test_model` is used to test the fidelity given by the currently
        # trained parameters. It's called at regular intervals during
        # the computation, and is the value shown in the dynamically
        # updated plot that is shown when the training is ongoing.
        self.test_model = theano.function(
            inputs=[],
            outputs=self.cost,
            updates=None,
            givens={self.net.inputs: self.vars['test_inputs'],
                    self.net.outputs: self.vars['test_outputs']})
        print(' done.')

    def _run(self, save_parameters=True, len_shown_history=200):
        # generate testing states
        self.refill_test_data()
        self._compile_model()

        n_epochs = self.hyperpars['n_epochs']
        # initialize log
        self.log['fidelities'] = np.zeros(n_epochs)
        if save_parameters:
            self.log['parameters'] = np.zeros((
                n_epochs, len(self.vars['parameters'].get_value())))
        # run epochs
        for n_epoch in range(n_epochs):
            self.log['n_epoch'] = n_epoch
            self.train_epoch()
            self.test_epoch(save_parameters=save_parameters)
            self._update_fig(len_shown_history)
            # stop if fidelity 1 is obtained
            if self.log['fidelities'][n_epoch] == 1:
                print('Fidelity 1 obtained, stopping.')
                break
            # update learning rate
            self.vars['learning_rate'].set_value(
                self.hyperpars['initial_learning_rate'] / (
                    1 + self.hyperpars['decay_rate'] * n_epoch))

    def run(self, save_parameters=True, len_shown_history=200,
            save_after=None):
        """
        Start the optimization.

        Parameters
        ----------
        save_parameters : bool, optional
            If True, the entire history of the parameters is stored.
        len_shown_history : int, optional
            If not None, the figure showing the fidelity for every epoch
            only shows the last `len_shown_history` epochs.
        save_after : str, optional
            If not None, it is used to save the results to file.
        """
        args = locals()
        # catch abort to stop training at will
        try:
            self._run(args)
        except KeyboardInterrupt:
            pass

        if save_after is not None:
            self._save_results()

    def plot_parameters_history(self, return_fig=False, return_df=False,
                                online=False):
        import cufflinks
        names = [par.name for par in self.net.free_parameters]
        df = pd.DataFrame(self._get_meaningful_history()['parameters'])
        new_col_names = dict(zip(range(df.shape[1]), names))
        df.rename(columns=new_col_names, inplace=True)
        if return_df:
            return df

        return df.iplot(asFigure=return_fig, online=online)
