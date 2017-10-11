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
from .theano_qutils import TheanoQstates


def _random_input_states(num_states, num_qubits):
    """Generate a bunch of random input ket states with qutip.

    Returns
    -------
    A list of `num_states` elements, with each element a `qutip.Qobj`
    of shape `(2**num_qubits, 1)`.
    """
    # `rand_ket_haar` seems to be slightly faster than `rand_ket`
    # The efficiency of this function can probably be dramatically improved.
    length_inputs = 2 ** num_qubits
    qutip_dims = [[2 for _ in range(num_qubits)],
                  [1 for _ in range(num_qubits)]]
    return [
        qutip.rand_ket_haar(length_inputs, dims=qutip_dims)
        for _ in range(num_states)
    ]

class TargetGateNotGivenError(Exception):
    pass


class QubitNetworkModel(QubitNetwork):
    """Handling of theano graph buliding on top of the QubitNetwork.

    Here we add the theano variables and functions to compute fidelity
    and so on.
    """
    def __init__(self, num_qubits=None,
                 interactions=None,
                 net_topology=None,
                 sympy_expr=None,
                 free_parameters_order=None,
                 initial_values=None):
        # Initialize `QubitNetwork` parent
        super().__init__(num_qubits=num_qubits,
                         interactions=interactions,
                         net_topology=net_topology,
                         sympy_expr=sympy_expr,
                         free_parameters_order=free_parameters_order)
        # attributes initialization
        self.initial_values = self._set_initial_values(initial_values)
        self.parameters, self.hamiltonian_model = self._build_theano_graph()
        # self.inputs and self.outputs are the holders for the training/testing
        # inputs and corresponding output states. They are used to build
        # the theano expression for the `fidelity`.
        self.inputs = T.dmatrix('inputs')
        self.outputs = T.dmatrix('outputs')

    def compute_evolution_matrix(self):
        """Compute matrix exponential of iH."""
        return T.slinalg.expm(self.hamiltonian_model)

    def generate_training_states_decision_problem(self,
                                                  num_states,
                                                  num_qubits_input1,
                                                  num_qubits_input2,
                                                  num_qubits_answer,
                                                  decision_fn=None,
                                                  ancillae_state=None):
        num_qubits_inputs = num_qubits_input1 + num_qubits_input2
        # abort if there are not enough qubits in the whole network
        if (num_qubits_inputs + num_qubits_answer > self.num_qubits):
            raise ValueError('Not enough qubits in the network.')
        # generate two random sets of input states
        inputs1 = _random_input_states(num_states, num_qubits_input1)
        inputs2 = _random_input_states(num_states, num_qubits_input2)
        # `decision_fn` is the function computing the answer qubit state
        # that we want to be associated with a given pair of inputs
        if decision_fn is None:
            def decision_fn(input1, input2):
                if qutip.fidelity(input1, input2) > 0.8:
                    return qutip.basis(2, 1)
                else:
                    return qutip.basis(2, 0)
        if ancillae_state is None:
            ancillae_state = qutip.basis(2, 0)
        # initial state of answer qubits
        answer_qubits_init = qutip.tensor(
            *([qutip.basis(2, 0)] * num_qubits_answer))
        # compute the states we want to be given in the answer qubit(s)
        answers = [decision_fn(input1, input2)
                   for input1, input2 in zip(inputs1, inputs2)]
        # compute number of remaining qubits, to be used as ancillary (
        # or "processor") qubits
        num_ancillae = self.num_qubits - num_qubits_inputs - num_qubits_answer
        ancillae_states = qutip.tensor(*([ancillae_state] * num_ancillae))
        # complete input states with ancillae and convert to big real
        training_inputs = []
        training_outputs = []
        for states in enumerate(zip(inputs1, inputs2, answers)):
            input1, input2, answer = states
            full_input = qutip.tensor(answer_qubits_init, input1, input2,
                                      ancillae_states)
            training_inputs.append(complex2bigreal(full_input))
            training_outputs.append(complex2bigreal(answer))
        training_inputs = np.asarray(training_inputs)
        training_outputs = np.asarray(training_outputs)
        return training_inputs, training_outputs

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

    def _build_theano_graph(self):
        """Build theano object corresponding to the Hamiltonian model.

        The free parameters in the output graphs are taken from the sympy
        free symbols in the Hamiltonian, stored in `self.free_parameters`.
        This is to be done regardless of what we need the network for
        (that is, both if we want to traing it to act as a gate on a
        subset of the qubits or to solve decision problems or whatever
        else).
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

    def net_parameters_to_dataframe(self, stringify_index=False):
        """
        Take parameters from a QubitNetwork object and put it in DataFrame.

        Parameters
        ----------
        stringify_index : bool
            If True, instead of a MultiIndex the output DataFrame will have
            a single index of strings, built applying `df.index.map(str)` to
            the original index structure.

        Returns
        -------
        A `pandas.DataFrame` with the interaction parameters ordered by
        qubits on which they act and type (interaction direction).
        """
        interactions, values = self.free_parameters, self.parameters.get_value()
        # now put everything in dataframe
        return pd.DataFrame({
            'interaction': interactions,
            'value': values
        }).set_index('interaction')
        # OLD STUFF, POSSIBLY OBSOLETE
        parameters = self.get_interactions_with_Js()
        qubits = []
        directions = []
        values = []
        for key, value in parameters.items():
            try:
                qubits.append(tuple(key[0]))
            except TypeError:
                qubits.append((key[0], ))
            directions.append(key[1])
            values.append(value)

        pars_df = pd.DataFrame({
            'qubits': qubits,
            'directions': directions,
            'values': values
        }).set_index(['qubits', 'directions']).sort_index()
        if stringify_index:
            pars_df.index = pars_df.index.map(str)
        return pars_df
    
    def plot_net_parameters(self, sort_index=True, plotly_online=False,
                            mode='lines+markers+text',
                            overlay_hlines=None,
                            asFigure=False, **kwargs):
        """Plot the current values of the parameters of the network."""
        import cufflinks
        import plotly
        df = self.net_parameters_to_dataframe()
        # stringify index (otherwise error is thrown by plotly)
        df.index = df.index.map(str)
        # optionally sort the index, grouping together self-interactions
        # if sort_index:
        #     def sorter(elem):
        #         return len(elem[0][0])
        #     sorted_data = sorted(list(df.iloc[:, 0].to_dict().items()),
        #                          key=sorter)
        #     x, y = tuple(zip(*sorted_data))
        #     df = pd.DataFrame({'x': x, 'y': y}).set_index('x')
        #     df.index = df.index.map(str)
        # decide online/offline
        if plotly_online:
            cufflinks.go_online()
        else:
            cufflinks.go_offline()
        # draw overlapping horizontal lines for reference if asked
        if overlay_hlines is None:
            overlay_hlines = np.arange(-np.pi, np.pi, np.pi / 2)
            # return df.iplot(kind='scatter', mode=mode, size=6,
            #                 title='Values of parameters',
            #                 asFigure=asFigure, **kwargs)
        from .plotly_utils import hline
        fig = df.iplot(kind='scatter', mode=mode, size=6,
                       title='Values of parameters',
                       text=df.index.tolist(),
                       asFigure=True, **kwargs)
        fig.layout.shapes = hline(0, len(self.free_parameters),
                                    overlay_hlines, dash='dash')
        fig.data[0].textposition = 'top'
        fig.data[0].textfont = dict(color='white', size=13)
        if asFigure:
            return fig
        else:
            return plotly.offline.iplot(fig)

    def generate_training_states(self, *args):
        """Generate training input/output pairs."""
        raise NotImplementedError('Subclasses must override this method.')

    def fidelity_test(self, *args):
        """Test the fidelity function using a different method."""
        raise NotImplementedError('Subclasses must override fidelity_test().')

    def fidelity(self, *args):
        """Compute the cost function of the model."""
        raise NotImplementedError('Subclasses must override fidelity().')


class QubitNetworkGateModel(QubitNetworkModel):
    """Model to be used for training network to reproduce a gate.

    This is the class to be used to train the network to reproduce a
    target gate on a subset of the qubits (the "system" qubits).
    """
    # pylint: disable=W0221
    def __init__(self, num_qubits=None, num_system_qubits=None,
                 interactions=None,
                 net_topology=None,
                 sympy_expr=None,
                 free_parameters_order=None,
                 ancillae_state=None,
                 initial_values=None,
                 target_gate=None):
        super().__init__(num_qubits=num_qubits,
                         interactions=interactions,
                         net_topology=net_topology,
                         sympy_expr=sympy_expr,
                         free_parameters_order=free_parameters_order)
        # parameters initialization
        self.ancillae_state = None  # initial values for ancillae (if any)
        self.num_system_qubits = None  # number of input/output qubits
        self.target_gate = target_gate
        # Build the initial state of the ancillae, if there are any
        if num_system_qubits is None:
            self.num_system_qubits = self.num_qubits
        else:
            self.num_system_qubits = num_system_qubits
        if self.num_system_qubits < self.num_qubits:
            self._initialize_ancillae(ancillae_state)

    def _initialize_ancillae(self, ancillae_state):
        """Initialize ancillae states, as a qutip.Qobj object.

        The generated state has every ancillary qubit in the 0 state,
        unless otherwise specified.
        """
        num_ancillae = self.num_qubits - self.num_system_qubits
        if ancillae_state is not None:
            raise NotImplementedError('Custom specification of ancillae'
                                      ' state not implemented yet.')
        state = qutip.tensor([qutip.basis(2, 0)
                              for _ in range(num_ancillae)])
        self.ancillae_state = state

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
        # `output_states` are the obtained output states, while
        # `self.outputs` are the output states we want (the training ones).
        states = TheanoQstates(self.inputs)
        states.evolve_all_kets(self.compute_evolution_matrix())
        num_ancillae = self.num_qubits - self.num_system_qubits
        fidelities = states.fidelities(self.outputs, num_ancillae)
        if return_mean:
            return T.mean(fidelities)
        else:
            return fidelities

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
              The former span the whole space while the latter only the
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
        if self.target_gate is None:
            raise TargetGateNotGivenError('Target gate not set yet.')

        # 1) Generate random input states over system qubits
        # `rand_ket_haar` seems to be slightly faster than `rand_ket`
        # length_inputs = 2 ** self.num_system_qubits
        # qutip_dims = [[2 for _ in range(self.num_system_qubits)],
        #               [1 for _ in range(self.num_system_qubits)]]
        # training_inputs = [
        #     qutip.rand_ket_haar(length_inputs, dims=qutip_dims)
        #     for _ in range(num_states)
        # ]
        training_inputs = _random_input_states(num_states,
                                               self.num_system_qubits)
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

    def get_current_gate(self, return_qobj=True):
        """Return the gate implemented by current interaction values.

        The returned value is a numpy ndarray, or a qutip Qobj if
        requested through the `return_qobj` parameter.
        """
        gate = scipy.linalg.expm(-1j * self.get_current_hamiltonian())
        if return_qobj:
            return qutip.Qobj(gate, dims=[[2] * self.num_qubits] * 2)
        return gate
