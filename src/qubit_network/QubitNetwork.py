import itertools
from collections import OrderedDict
import os
import qutip
# import matplotlib.pyplot as plt
import scipy.linalg
import numpy as np
import theano
import theano.tensor as T
import theano.tensor.slinalg  # for expm()
import theano.tensor.nlinalg  # for trace()
from utils import chars2pair, complex2bigreal, bigreal2complex, chop


class QubitNetwork:
    def __init__(self, num_qubits, system_qubits=None,
                 interactions='all',
                 ancillae_state=None,
                 target_gate=None,
                 net_topology=None,
                 J=None):
        # `self.num_qubits` is the TOTAL number of qubits in the
        # network, regardless of them being system or ancilla qubits
        self.num_qubits = num_qubits
        # Define which qubits belong to the system. The others are all
        # assumed to be ancilla qubits. If `system_qubits` was not
        # explicitly given it is assumed that half of the qubits are the
        # system and half are ancillae
        if system_qubits is None:
            self.system_qubits = tuple(range(num_qubits // 2))
        elif (isinstance(system_qubits, list) and
                all(qb < num_qubits for qb in system_qubits)):
            self.system_qubits = tuple(system_qubits)
        elif isinstance(system_qubits, int) and system_qubits <= num_qubits:
            self.system_qubits = tuple(range(system_qubits))
        else:
            raise ValueError('Invalid value for system_qubits.')

        # `self.target_gate` is given a value when the net is being
        # trained, for example by `sgd_optimization`. It is used simply
        # to keep track of what the network was trained to reproduce.
        self.target_gate = target_gate

        # it will still be useful in the following to have direct access
        # to the number of ancilla and system qubits
        self.num_ancillae = self.num_qubits - len(self.system_qubits)
        self.num_system_qubits = len(self.system_qubits)

        # we store all the possible pairs for convenience
        self.pairs = list(itertools.combinations(range(self.num_qubits), 2))

        self.net_topology = net_topology
        if net_topology is not None:
            self.net_topology_symbols = sorted(set(self.net_topology.values()))

        # `parse_interactions` fills the `self.interactions`,
        # `self.num_interactions` and `self.num_self_interactions`
        # variables
        self.parse_interactions(interactions)

        # Build the initial state of the ancillae, if there are any
        if self.num_ancillae > 0:
            if ancillae_state is None:
                self.ancillae_state = self.build_ancilla_state()
            else:
                self.ancillae_state = ancillae_state

        # self.J is the set of parameters that we are going to train
        if J is None:
            if net_topology is None:
                self.J = theano.shared(
                    value=np.random.randn(self.num_interactions),
                    name='J',
                    borrow=True
                )
            else:
                num_symbols = len(set(net_topology.values()))
                self.J = theano.shared(
                    value=np.random.randn(num_symbols),
                    name='J',
                    borrow=True
                )
        else:
            # if a `net_topology` has been given, check consistency
            if net_topology is not None:
                num_symbols = len(set(s for s in net_topology.values()))
                if np.asarray(J).shape[0] != num_symbols:
                    raise ValueError('The number of specified parameters is '
                                     'not consistent with the value of `net'
                                     '_topology`.')
            self.J = theano.shared(
                value=np.asarray(J),
                name='J',
                borrow=True
            )

    def parse_interactions(self, interactions):
        """Sets the value of `self.interactions`. Returns None.

        The input-given value of `interactions` is parsed, and the value
        of `self.interactions` is accordingly set to describe the whole
        set of (self-)interactions in the network.

        Parameters
        ----------
        interactions: used to specify the active interactions.
            - 'all': all possible pairwise interactions are active.
            - ('all', directions): for each pair of qubits the active
                interactions are all and only those specified in `directions`.
            - {pair1: dir1, pair2: dir2, ...}: explicitly specify all the
                active interactions.

        Returns
        -------
        None
        """
        outints = []

        if self.net_topology is not None:
            # the expected form of `self.net_topology` is a dictionary like
            # the following:
            # {
            #   ((1, 2), 'xx'): 'a',
            #   ((1, 3), 'xx'): 'a',
            #   ((2, 3), 'xx'): 'a',
            #   ((1, 2), 'xy'): 'b',
            # }

            outints = list(self.net_topology.keys())

            # not clear if it even makes sense to use `self.interactions`
            # if a `net_topology` has been given..
            self.interactions = outints
            self.num_interactions = len(set(self.net_topology.values()))
            return

        elif interactions == 'all':
            # create all self-interaction terms
            for qubit in range(self.num_qubits):
                for s in ['x', 'y', 'z']:
                    outints.append((qubit, s))
            # create all interactions terms
            for pair in self.pairs:
                for s1 in ['x', 'y', 'z']:
                    for s2 in ['x', 'y', 'z']:
                        outints.append((pair, s1 + s2))

        elif isinstance(interactions, tuple):
            if interactions[0] == 'all':
                # here we need to first iterate over the interaction
                # types because they can be either self- or pairwise
                # interactions, and depending on this they must be
                # associated to single or pairs of qubits, respectively.
                for d in interactions[1]:
                    if len(d) == 1:
                        for qubit in range(self.num_qubits):
                            outints.append((qubit, d))
                    elif len(d) == 2:
                        for pair in self.pairs:
                            outints.append((pair, d))
        elif isinstance(interactions, list):
            outints = interactions
        else:
            raise ValueError(
                'Invalid value given for interactions.',
                interactions)

        num_self_interactions = 0
        for q, d in outints:
            if len(d) == 1:
                num_self_interactions += 1

        self.interactions = outints
        self.num_interactions = len(outints)
        self.num_self_interactions = num_self_interactions

    def build_H_factor(self, pair):
        term = [qutip.qeye(2) for _ in range(self.num_qubits)]

        sigmas = [qutip.qeye(2),
                  qutip.sigmax(), qutip.sigmay(), qutip.sigmaz()]

        target, d = pair
        # if `d` indicates a self-interaction..
        if len(d) == 1:
            term[target] = sigmas[chars2pair(d)[0]]
        # if `d` indicates a pairwise interaction..
        elif len(d) == 2:
            term[target[0]] = sigmas[chars2pair(d)[0]]
            term[target[1]] = sigmas[chars2pair(d)[1]]

        return complex2bigreal(-1j * qutip.tensor(term).data.toarray())

    def build_H_factors(self, symbolic_result=True, J=None):
        if J is None:
            if symbolic_result:
                _J = self.J
            else:
                _J = self.J.get_value()
        else:
            _J = J

        dim_real_space = 2 * 2 ** self.num_qubits
        if self.net_topology is None:

            factors = np.zeros(
                (self.num_interactions, dim_real_space, dim_real_space),
                dtype=np.float64
            )
            for idx, pair in enumerate(self.interactions):
                factors[idx] = self.build_H_factor(pair)

            if symbolic_result:
                # return the dot product between `self.J` and `factors`,
                # amounting to the sum over `i` of `self.J[i] * factors[i]`
                return T.tensordot(_J, factors, axes=1)
            else:
                return np.tensordot(_J, factors, axes=1)
        else:
            # the expected form of `self.net_topology` is a dictionary like
            # the following:
            # {
            #   ((1, 2), 'xx'): 'a',
            #   ((1, 3), 'xx'): 'a',
            #   ((2, 3), 'xx'): 'a',
            #   ((1, 2), 'xy'): 'b',
            # }

            # symbols = []
            # for symb in self.net_topology.values():
            #     if symb not in symbols:
            #         symbols.append(str(symb))
            # symbols.sort()
            symbols = sorted(set(self.net_topology.values()))

            factors = np.zeros(
                (self.num_interactions, dim_real_space, dim_real_space),
                dtype=np.float64
            )

            # The number of elements in `symbols` should be equal to
            # `self.num_interactions`, computed by `parse_interactions`.
            # Note that is the code below that determines to what
            # interaction does the i-th element of `self.J` correspond
            # to.
            # The i-th element of `self.J` will correspond to the
            # interactions terms associated to the i-th symbol listed
            # in `symbols` (after sorting).
            for idx, symb in enumerate(symbols):
                for pair, label in self.net_topology.items():
                    if str(label) == symb:
                        factors[idx] += self.build_H_factor(pair)

            if symbolic_result:
                return T.tensordot(_J, factors, axes=1)
            else:
                return np.tensordot(_J, factors, axes=1)

    def build_ancilla_state(self):
        """Returns an initial ancilla state, as a qutip.Qobj object.

        The generated state has every ancillary qubit in the up position.
        """
        state = qutip.tensor([qutip.basis(2, 0)
                              for _ in range(self.num_ancillae)])
        return state

    def generate_training_data(self, target_gate=None, size=10):
        """Generates a set of training data for the QubitNetwork net.

        Returns
        -------
        A tuple with two elements: training vectors and labels.
        NOTE: The training and target vectors have different lengths!
              The latter span the whole space while the former only the
              system one.

        training_states: an array of vectors.
            Each vector represents a state in the full system+ancilla space,
            in big real form.
        target_states: an array of vectors.
            Each vector represents a state spanning only the system qubits,
            in big real form. Every such state is generated by evolving
            the corresponding `training_state` through the matrix
            `target_unitary`.
        """

        system_size = 2 ** self.num_system_qubits

        if target_gate is None:
            if self.target_gate is None:
                raise ValueError('No target gate has been specified.')
            target_gate = self.target_gate
        # generate a number `size` of normalized vectors, each one of
        # length `self.num_system_qubits`.
        training_states = [qutip.rand_ket(system_size) for _ in range(size)]
        qutip_dims = [[2 for _ in range(self.num_system_qubits)],
                      [1 for _ in range(self.num_system_qubits)]]
        for idx in range(len(training_states)):
            training_states[idx].dims = qutip_dims

        # evolve all training states
        if not isinstance(target_gate, qutip.Qobj):
            raise TypeError('`target_gate` should be a qutip object.')

        target_states = [target_gate * psi for psi in training_states]

        # now compute the tensor product between every element of
        # `target_states` and the ancillae state, IF there are ancillae
        # over which we have to trace over after
        if self.num_ancillae > 0:
            training_states = [qutip.tensor(psi, self.ancillae_state)
                               for psi in training_states]

        # convert all the computed states in big real form
        training_states = [complex2bigreal(psi) for psi in training_states]
        target_states = [complex2bigreal(psi) for psi in target_states]

        return np.asarray(training_states), np.asarray(target_states)

    def save_to_file(self, outfile):
        """ Saves the currently trained net in a file. """
        import pickle
        data = {
            'num_qubits': self.num_qubits,
            'num_system_qubits': self.num_system_qubits,
            'interactions': self.interactions,
            'target_gate': self.target_gate,
            'net_topology': self.net_topology,
            'J': self.J.get_value()
        }
        if not os.path.isabs(outfile):
            outfile = os.path.join(os.getcwd(), outfile)
        with open(outfile, 'wb') as file:
            pickle.dump(data, file)

    def save_gate_to_file(self, outfile):
        """ Prints the target get of the net to a file (for debug). """
        np.savetxt(outfile, self.get_current_gate(), delimiter=',')

    # def tuple_to_interaction_index(self, pair):
    #     self.interactions.index(pair)

    def tuple_to_J_index(self, interaction):
        if self.net_topology is None:
            # if `pair` is a self-interaction
            return self.interactions.index(interaction)
        else:
            raise NotImplementedError('I didn\'t implement this yet, sorry!')

    def J_index_to_interaction(self, index):
        """
        Gives the tuple representing the interaction `self.J[index]`.

        The set of (self-)interaction parameters of a qubit network is
        stored in the `self.J` variable of the `QubitNetwork` instance.
        This function is a utility to easily recover which interaction
        corresponds to the given index.

        If `self.net_topology` has not been given, this is done by
        simply looking at `self.interactions`, which lists all (and
        only) the active interactions in the network.
        If a custom `self.net_topology` was given, then its value is
        used to recover the (self-)interaction corresponding to the `J`
        element. The output will therefore in this case be a list of
        tuples, each one representing a single interaction.
        """
        if self.net_topology is None:
            return self.interactions[index]
        else:
            symbols = sorted(set(self.net_topology.values()))
            interactions = []
            for interaction, symb in self.net_topology.items():
                if symb == symbols[index]:
                    interactions.append(interaction)
            return interactions

    def remove_interaction(self, interaction_tuple):
        """Removes the specified interaction from the network."""
        if self.net_topology is None:
            # in this case we just remove the specified interaction from
            # the `self.interactions` list, and the corresponding entry
            # in `self.J`
            idx = self.interactions.index(interaction_tuple)
            Js = self.J.get_value()
            del self.interactions[idx]
            del Js[idx]
            self.J.set_value(Js)
        else:
            # idx = list(self.net_topology.keys()).index(interaction_tuple)
            symbol = self.net_topology[interaction_tuple]
            all_interactions = [k for k, v in self.net_topology.items()
                                if v == symbol]

            # if there are interactions associated to the same symbol..
            if len(all_interactions) > 1:
                # then we just remove the corresponding entry in the
                # `self.net_topology` variable (no need to change the
                # value of `self.J`)
                del self.net_topology[interaction_tuple]
            # if there are interactions associated to the same parameter
            elif len(all_interactions) == 1:
                # then we also remove the corresponding entry of
                # `self.J`, in addition to removing the entry in
                # `self.net_topology`
                symbols = self.net_topology_symbols
                Js = self.J.get_value()
                del Js[symbols.index(symbol)]
                self.J.set_value(Js)
                del self.net_topology_symbols[symbols.index(symbol)]

    def get_current_gate(self, return_Qobj=True, chop_eps=None):
        """Returns the currently produced unitary, in complex form."""
        gate = self.build_H_factors(symbolic_result=False)
        gate = scipy.linalg.expm(gate)
        gate = bigreal2complex(gate)
        if chop_eps is not None:
            gate = chop(gate, chop_eps)

        if return_Qobj:
            return qutip.Qobj(gate, dims=[[2] * self.num_qubits] * 2)
        else:
            return gate

    def get_interactions_with_Js(self, renormalize_parameters=False):
        """
        Gives a dict associating each interaction to the correspoding J.

        Parameters
        ----------
        renormalize_parameters : bool
            If True, multiplies the pairwise interactions by 4 and the
            self-interactions by 2.
        """
        if self.net_topology is None:
            outdict = OrderedDict()
            Js = self.J.get_value()
            for interaction, J in zip(self.interactions, Js):
                # if `renormalize_parameter` is set to True the results
                # are printed using the convention in which the terms
                # in the Hamiltonian are 1/4 for the pairwise
                # interactions and 1/2 for the self-interactions.
                if renormalize_parameters:
                    if len(interaction[1]) == 2:
                        outdict[interaction] = 4 * J
                    elif len(interaction[1]) == 1:
                        outdict[interaction] = 2 * J
                else:
                    outdict[interaction] = J
            return outdict
        else:
            symbols = self.net_topology_symbols
            outdict = OrderedDict()
            Js = self.J.get_value()
            for idx, symbol in enumerate(symbols):
                interactions = []
                renormalize = 0
                for interaction, label in self.net_topology.items():
                    if label == symbol:
                        interactions.append(interaction)
                        # the following mess is to check that it makes
                        # sense to renormalize the parameters associated
                        # with a symbol: if a symbol is associated to
                        # both pairwise and self- interactions then we
                        # cannot consistently renormalize the Js.
                        if renormalize_parameters:
                            if len(interaction[1]) == 2 and renormalize == 2:
                                renormalize = 0
                                print('Can\' properly renormalize the paramete'
                                      'rs, reverting to non-renormalized form.'
                                      )
                            elif len(interaction[1]) == 1 and renormalize == 4:
                                renormalize = 0
                                print('Can\' properly renormalize the paramete'
                                      'rs, reverting to non-renormalized form.'
                                      )
                            elif renormalize == 0:
                                if len(interaction[1]) == 2:
                                    renormalize = 4
                                elif len(interaction[1]) == 1:
                                    renormalize = 2

                if not renormalize_parameters:
                    renormalize = 1
                outdict[tuple(interactions)] = Js[idx] * renormalize

            return outdict

    def test_fidelity(self,
                      states=None, target_states=None,
                      target_gate=None,
                      n_samples=10):
        """Computes an average fidelity with the current values of J."""
        if target_gate is None:
            if self.target_gate is None:
                raise ValueError('No target gate has been specified')
            else:
                target_gate = self.target_gate

        if states is None or target_states is None:
            states, target_states = self.generate_training_data(
                target_gate, n_samples)

        fidelity = theano.function(
            inputs=[],
            outputs=self.fidelity(states, target_states)
        )
        return fidelity()

    def test_fidelity_without_theano(self):
        """
        Computes the fidelity with random states, using only qutip.

        This function can be used to double check the output given in the
        optimization process. The whole computation is here done using
        high level `qutip` functions.
        """
        gate = self.get_current_gate()
        psi_in = qutip.rand_ket(2 ** self.num_system_qubits)
        psi_in.dims = [
            [2] * self.num_system_qubits, [1] * self.num_system_qubits]
        Psi_in = qutip.tensor(psi_in, qutip.basis(2, 0))

        Psi_out = gate * Psi_in
        dm_out = Psi_out.ptrace([0, 1, 2])

        return (psi_in.dag() * self.target_gate.dag() * dm_out *
                self.target_gate * psi_in)

    # `fidelity_1s` computes the fidelity over a single pair
    # of state and target state, as opposite as the computation of the
    # mini-batch averages (using `theano.scan`) done by `fidelity`.
    def fidelity_1s(self, state, target_state, J=None):
        """Compute the fidelity from a single pair of states."""
        # this builds the Hamiltonian of the system (in big real matrix
        # form), already multiplied with the 1j factor and ready for
        # exponentiation.
        H = self.build_H_factors(symbolic_result=True, J=J)
        # expH is the unitary evolution of the system
        expH = T.slinalg.expm(H)
        Uxpsi = T.dot(expH, state).reshape((state.shape[0], 1))
        Uxpsi_real = Uxpsi[:Uxpsi.shape[0] // 2]
        Uxpsi_imag = Uxpsi[Uxpsi.shape[0] // 2:]
        dm_real = Uxpsi_real * Uxpsi_real.T + Uxpsi_imag * Uxpsi_imag.T
        dm_imag = Uxpsi_imag * Uxpsi_real.T - Uxpsi_real * Uxpsi_imag.T

        def col_fn(col_idx, row_idx, matrix):
            subm_dim = 2 ** self.num_ancillae
            return T.nlinalg.trace(
                matrix[row_idx * subm_dim:(row_idx + 1) * subm_dim,
                       col_idx * subm_dim:(col_idx + 1) * subm_dim])

        def row_fn(row_idx, matrix):
            results, _ = theano.scan(
                fn=col_fn,
                sequences=T.arange(matrix.shape[1] // 2 ** self.num_ancillae),
                non_sequences=[row_idx, matrix]
            )
            return results

        dm_real_traced, _ = theano.scan(
            fn=row_fn,
            sequences=T.arange(dm_real.shape[0] // 2 ** self.num_ancillae),
            non_sequences=[dm_real]
        )
        dm_imag_traced, _ = theano.scan(
            fn=row_fn,
            sequences=T.arange(dm_imag.shape[0] // 2 ** self.num_ancillae),
            non_sequences=[dm_imag]
        )

        target_state_real = target_state[:target_state.shape[0] // 2, None]
        target_state_imag = target_state[target_state.shape[0] // 2:, None]
        target_dm_real = (target_state_real * target_state_real.T +
                          target_state_imag * target_state_imag.T)
        target_dm_imag = (target_state_imag * target_state_real.T -
                          target_state_real * target_state_imag.T)

        prod_real = (T.dot(dm_real_traced, target_dm_real) -
                     T.dot(dm_imag_traced, target_dm_imag))
        tr_real = T.nlinalg.trace(prod_real)

        # prod_imag = (T.dot(dm_real_traced, target_dm_imag) +
        #              T.dot(dm_imag_traced, target_dm_real))
        # tr_imag = T.nlinalg.trace(prod_imag)

        # tr_abs = T.sqrt(tr_real ** 2 + tr_imag ** 2)

        # guess we should show why this is correct?
        return tr_real

    def fidelity(self, states, target_states, return_mean=True):
        """The cost function of the model.

        The states given in `states` are evolved through the network
        with interactions parameters `self.J`, then traced over the
        ancilla degrees of freedom. Every state obtained in this way
        is then projected over the corresponding target state
        (corresponding element of `target_states`), thus computing the
        fidelity for every state.
        The average value of all the fidelities thus computes is the
        returned value.
        A properly formatted set of `states` and `target_states` can be
        generated by `self.generate_training_data`.

        This function does not compute the actual value of the fidelity,
        but rather builds the computational graph for theano.

        Parameters
        ----------
        states : numpy array, shape (n_states, dim_states)
            The initial states to be evolved into the network.
            Typically every element is the tensor product between a
            randomly generated ket state, and the ancillae states.
            The states are expected to be in "big real form", that is,
            every state is actually an array generated by concatenating
            the real and imaginary parts of the original ket vector
            (in this order).
            Every state is expected to span the *whole* qubit network,
            thus having a length of 2 * (2 ** self.num_qubits).

        target_states : numpy array, shape (n_states, dim_target_states)
            The ideal result to which to compare the elements of
            `states`.
            Every state in `target_states` is expected to be the result
            of evolving the corresponding state in `states` with the
            target gate that the training is trying to make the network
            implement.
        return_mean : bool
            If True, returns the average values of the fidelities.
            If False, returns the computed array of fidelities

        Returns
        -------
        A theano function, to be used for the MSGD algorithm
        """

        # this builds the Hamiltonian of the system (in big real matrix form),
        # already multiplied with the -1j factor and ready for exponentiation
        H = self.build_H_factors()
        # expH is the unitary evolution of the system
        expH = T.slinalg.expm(H)

        # `expH_times_state` is the full output state given by the qubit
        # network.
        # `state` in general is a matrix (array of state vectors) so
        # that `expH_times_state` is also a matrix with a number of
        # rows equal to the number of training vectors. Every row
        # of this matrix is a state evolved according to the gate
        # implemented by the network with the current interactions
        # parameters.
        expH_times_state = T.tensordot(expH, states, axes=([1], [1])).T

        # `col_fn` and `row_fn` are used inside `compute_fidelities` to
        # compute the partial traces
        def col_fn(col_idx, row_idx, matrix):
            subm_dim = 2 ** self.num_ancillae
            return T.nlinalg.trace(
                matrix[row_idx * subm_dim:(row_idx + 1) * subm_dim,
                       col_idx * subm_dim:(col_idx + 1) * subm_dim])

        def row_fn(row_idx, matrix):
            results, _ = theano.scan(
                fn=col_fn,
                sequences=T.arange(matrix.shape[1] // 2 ** self.num_ancillae),
                non_sequences=[row_idx, matrix]
            )
            return results

        # `compute_fidelities` is to be called by the immediately
        # following `theano.scan`. It returns the fidelity between the
        # result of evolving `states[i]` and `target_states[i]`.
        def compute_fidelities(i, matrix, target_states):
            # here matrix[i] is the i-th training state after evolution
            # through exp(-1j * H)
            Uxpsi = matrix[i].reshape((matrix[i].shape[0], 1))
            Uxpsi_real = Uxpsi[:Uxpsi.shape[0] // 2]
            Uxpsi_imag = Uxpsi[Uxpsi.shape[0] // 2:]
            dm_real = Uxpsi_real * Uxpsi_real.T + Uxpsi_imag * Uxpsi_imag.T
            dm_imag = Uxpsi_imag * Uxpsi_real.T - Uxpsi_real * Uxpsi_imag.T

            dm_real_traced, _ = theano.scan(
                fn=row_fn,
                sequences=T.arange(dm_real.shape[0] // 2 ** self.num_ancillae),
                non_sequences=[dm_real]
            )
            dm_imag_traced, _ = theano.scan(
                fn=row_fn,
                sequences=T.arange(dm_imag.shape[0] // 2 ** self.num_ancillae),
                non_sequences=[dm_imag]
            )

            target_state = target_states[i]
            target_state_real = target_state[:target_state.shape[0] // 2, None]
            target_state_imag = target_state[target_state.shape[0] // 2:, None]
            target_dm_real = (target_state_real * target_state_real.T +
                              target_state_imag * target_state_imag.T)
            target_dm_imag = (target_state_imag * target_state_real.T -
                              target_state_real * target_state_imag.T)

            prod_real = (T.dot(dm_real_traced, target_dm_real) -
                         T.dot(dm_imag_traced, target_dm_imag))
            tr_real = T.nlinalg.trace(prod_real)

            # prod_imag = (T.dot(dm_real_traced, target_dm_imag) +
            #              T.dot(dm_imag_traced, target_dm_real))
            # tr_imag = T.nlinalg.trace(prod_imag)
            # tr_abs = T.sqrt(tr_real ** 2 + tr_imag ** 2)

            # guess we should show why this is correct?
            return tr_real

        # If no ancilla is present in the network, there is no need
        # to partial trace anything, so that the fidelity can be simply
        # computed projecting the evolution of every element of
        # `states` over the corresponding element of `target_states`,
        # and taking the squared modulus of this number.
        def fidelities_no_ptrace(i, states, target_states):
            state = states[i]
            target_state = target_states[i]
            state_real = state[:state.shape[0] // 2]
            state_imag = state[state.shape[0] // 2:]
            target_state_real = target_state[:target_state.shape[0] // 2]
            target_state_imag = target_state[target_state.shape[0] // 2:]

            fidelity_real = (T.dot(state_real, target_state_real) +
                             T.dot(state_imag, target_state_imag))
            fidelity_imag = (T.dot(state_real, target_state_imag) -
                             T.dot(state_imag, target_state_real))
            fidelity = fidelity_real ** 2 + fidelity_imag ** 2
            return fidelity

        # the function also supports the case in which there are no
        # ancillae over which to trace over.
        if self.num_ancillae == 0:
            fidelities, _ = theano.scan(
                fn=fidelities_no_ptrace,
                sequences=T.arange(expH_times_state.shape[0]),
                non_sequences=[expH_times_state, target_states]
            )
        else:
            fidelities, _ = theano.scan(
                fn=compute_fidelities,
                sequences=T.arange(expH_times_state.shape[0]),
                non_sequences=[expH_times_state, target_states]
            )

        if return_mean:
            # return the mean of the fidelities
            return T.mean(fidelities)
        else:
            return fidelities
