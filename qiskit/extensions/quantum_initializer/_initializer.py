# -*- coding: utf-8 -*-

# Copyright 2017, IBM.
#
# This source code is licensed under the Apache License, Version 2.0 found in
# the LICENSE.txt file in the root directory of this source tree.

"""
Initialize qubit registers to desired arbitrary state.
"""

import math
import numpy as np
import scipy

from qiskit.exceptions import QiskitError
from qiskit.circuit import QuantumCircuit
from qiskit.circuit import CompositeGate
from qiskit.circuit import Gate
from qiskit.extensions.standard.cx import CnotGate
from qiskit.extensions.standard.ry import RYGate
from qiskit.extensions.standard.rz import RZGate
from qiskit.extensions.standard.u3 import U3Gate
from qiskit.extensions.standard.x import XGate

_EPS = 1e-10  # global variable used to chop very small numbers to zero

class InitializeGate(CompositeGate):  # pylint: disable=abstract-method
    """Complex amplitude initialization.

    Class that implements the (complex amplitude) initialization of some
    flexible collection of qubit registers (assuming the qubits are in the
    zero state).

    Implements a recursive initialization algorithm including optimizations
    from "Synthesis of Quantum Logic Circuits" Shende, Bullock, Markov
    https://arxiv.org/abs/quant-ph/0406176v5

    Additionally implements some extra optimizations: remove zero rotations and
    double cnots.`

    It inherits from CompositeGate in the same way that the Fredkin (cswap)
    gate does. Therefore self.data is the list of gates (in order) that must
    be applied to implement this meta-gate.

    params = list of complex amplitudes
    qargs = list of qubits
    circ = QuantumCircuit or CompositeGate containing this gate
    """
    def __init__(self, params, qargs, circ=None):
        """Create new initialize composite gate."""
        num_qubits = math.log2(len(params))

        # Check if param is a power of 2
        if num_qubits == 0 or not num_qubits.is_integer():
            raise QiskitError("Desired vector not a positive power of 2.")

        self.num_qubits = int(num_qubits)

        # Check if number of desired qubits agrees with available qubits
        if len(qargs) != self.num_qubits:
            raise QiskitError("Number of complex amplitudes do not correspond "
                              "to the number of qubits.")

        # Check if probabilities (amplitudes squared) sum to 1
        if not math.isclose(sum(np.absolute(params) ** 2), 1.0,
                            abs_tol=_EPS):
            raise QiskitError("Sum of amplitudes-squared does not equal one.")

        super().__init__("init", params, qargs, circ)

        # call to generate the circuit that takes the desired vector to zero (up
        # to global phase, which is returned and conjugated, giving the
        # hypothetical left over global phase if zero vector and
        # all gates were perfect)
        self.global_phase = self.gates_to_uncompute().conjugate()
        
        # remove zero rotations and double cnots
        self.optimize_gates()
        # invert the circuit to create the desired vector from zero (assuming
        # the qubits are in the zero state)
        self.inverse()
        # do not set the inverse flag, as this is the actual initialize gate
        # we just used inverse() as a method to obtain it
        self.inverse_flag = False

    @property 
    def get_hypothetical_left_over_global_phase(self):
        """
        Return the hypothetical left over global phase shortfall, between what 
        the initialization circuit produces and what was asked for, assuming that
        the initial vector was perfectly zero and that all sub-gates do not
        introduce unaccounted for global phases.
        """
        return self.global_phase

    def nth_qubit_from_least_sig_qubit(self, nth):
        """
        Return the qubit that is nth away from the least significant qubit
        (LSB), so n=0 corresponds to the LSB.
        """
        # if LSB is first (as is the case with the IBM QE) and significance is
        # in order:
        return self.qargs[nth]
        # if MSB is first: return self.qargs[self.num_qubits - 1 - n]
        #  equivalent to self.qargs[-(n+1)]
        # to generalize any mapping could be placed here or even taken from
        # the user

    def reapply(self, circ):
        """Reapply this gate to the corresponding qubits in circ."""
        self._modifiers(circ.initialize(self.params, self.qargs))

    def gates_to_uncompute(self):
        """
        Call to populate the self.data list with gates that takes the
        desired vector to zero.
        """
        # kick start the peeling loop
        remaining_param = self.params

        for i in range(self.num_qubits):
            # work out which rotations must be done to disentangle the LSB
            # qubit (we peel away one qubit at a time)
            (remaining_param,
             thetas,
             phis) = InitializeGate._rotations_to_disentangle(remaining_param)

            # perform the required rotations to decouple the LSB qubit (so that
            # it can be "factored" out, leaving a
            # shorter amplitude vector to peel away)
            self._attach(self._multiplex(RZGate, i, phis))
            self._attach(self._multiplex(RYGate, i, thetas))

        return remaining_param[0] # Returns global phase

    @staticmethod
    def _rotations_to_disentangle(local_param):
        """
        Static internal method to work out Ry and Rz rotation angles used
        to disentangle the LSB qubit.
        These rotations make up the block diagonal matrix U (i.e. multiplexor)
        that disentangles the LSB.

        [[Ry(theta_1).Rz(phi_1)  0   .   .   0],
         [0         Ry(theta_2).Rz(phi_2) .  0],
                                    .
                                        .
          0         0           Ry(theta_2^n).Rz(phi_2^n)]]
        """
        remaining_vector = []
        thetas = []
        phis = []

        param_len = len(local_param)

        for i in range(param_len // 2):
            # Ry and Rz rotations to move bloch vector from 0 to "imaginary"
            # qubit
            # (imagine a qubit state signified by the amplitudes at index 2*i
            # and 2*(i+1), corresponding to the select qubits of the
            # multiplexor being in state |i>)
            (remains,
             add_theta,
             add_phi) = InitializeGate._bloch_angles(
                 local_param[2*i: 2*(i + 1)])

            remaining_vector.append(remains)

            # rotations for all imaginary qubits of the full vector
            # to move from where it is to zero, hence the negative sign
            thetas.append(-add_theta)
            phis.append(-add_phi)

        return remaining_vector, thetas, phis

    @staticmethod
    def _bloch_angles(pair_of_complex):
        """
        Static internal method to work out rotation to create the passed in
        qubit from the zero vector.
        """
        [a_complex, b_complex] = pair_of_complex
        # Force a and b to be complex, as otherwise numpy.angle might fail.
        a_complex = complex(a_complex)
        b_complex = complex(b_complex)
        mag_a = np.absolute(a_complex)
        final_r = float(np.sqrt(mag_a ** 2 + np.absolute(b_complex) ** 2))
        if final_r < _EPS:
            theta = 0
            phi = 0
            final_r = 0
            final_t = 0
        else:
            theta = float(2 * np.arccos(mag_a / final_r))
            a_arg = np.angle(a_complex)
            b_arg = np.angle(b_complex)
            final_t = a_arg + b_arg
            phi = b_arg - a_arg

        return final_r * np.exp(1.J * final_t/2), theta, phi

    def _multiplex(self, bottom_gate, bottom_qubit_index, list_of_angles):
        """
        Internal recursive method to create gates to perform rotations on the
        imaginary qubits: works by rotating LSB (and hence ALL imaginary
        qubits) by combo angle and then flipping sign (by flipping the bit,
        hence moving the complex amplitudes) of half the imaginary qubits
        (CNOT) followed by another combo angle on LSB, therefore executing
        conditional (on MSB) rotations, thereby disentangling LSB.
        """
        list_len = len(list_of_angles)
        target_qubit = self.nth_qubit_from_least_sig_qubit(bottom_qubit_index)

        # Case of no multiplexing = base case for recursion
        if list_len == 1:
            return bottom_gate(list_of_angles[0], target_qubit)

        local_num_qubits = int(math.log2(list_len)) + 1
        control_qubit = self.nth_qubit_from_least_sig_qubit(
            local_num_qubits - 1 + bottom_qubit_index)

        # calc angle weights, assuming recursion (that is the lower-level
        # requested angles have been correctly implemented by recursion
        angle_weight = scipy.kron([[0.5, 0.5], [0.5, -0.5]],
                                  np.identity(2 ** (local_num_qubits - 2)))

        # calc the combo angles
        list_of_angles = angle_weight.dot(np.array(list_of_angles)).tolist()
        combine_composite_gates = CompositeGate(
            "multiplex" + local_num_qubits.__str__(), [], self.qargs)

        # recursive step on half the angles fulfilling the above assumption
        combine_composite_gates._attach(
            self._multiplex(bottom_gate, bottom_qubit_index,
                            list_of_angles[0:(list_len // 2)]))

        # combine_composite_gates.cx(control_qubit,target_qubit) -> does not
        # work as expected because checks circuit
        # so attach CNOT as follows, thereby flipping the LSB qubit
        combine_composite_gates._attach(CnotGate(control_qubit, target_qubit))

        # implement extra efficiency from the paper of cancelling adjacent
        # CNOTs (by leaving out last CNOT and reversing (NOT inverting) the
        # second lower-level multiplex)
        sub_gate = self._multiplex(
            bottom_gate, bottom_qubit_index, list_of_angles[(list_len // 2):])
        if isinstance(sub_gate, CompositeGate):
            combine_composite_gates._attach(sub_gate.reverse())
        else:
            combine_composite_gates._attach(sub_gate)

        # outer multiplex keeps final CNOT, because no adjacent CNOT to cancel
        # with
        if self.num_qubits == local_num_qubits + bottom_qubit_index:
            combine_composite_gates._attach(CnotGate(control_qubit,
                                                     target_qubit))

        return combine_composite_gates

    @staticmethod
    def chop_num(numb):
        """
        Set very small numbers (as defined by global variable _EPS) to zero.
        """
        return 0 if abs(numb) < _EPS else numb


# ###############################################################
# Add needed functionality to other classes (it feels
# weird following the Qiskit convention of adding functionality to other
# classes like this ;),
#  TODO: multiple inheritance might be better?)


def reverse(self):
    """
    Reverse (recursively) the sub-gates of this CompositeGate. Note this does
    not invert the gates!
    """
    new_data = []
    for gate in reversed(self.data):
        if isinstance(gate, CompositeGate):
            new_data.append(gate.reverse())
        else:
            new_data.append(gate)
    self.data = new_data

    # not just a high-level reverse:
    # self.data = [gate for gate in reversed(self.data)]

    return self


QuantumCircuit.reverse = reverse
CompositeGate.reverse = reverse


def optimize_gates(self):
    """Remove Zero rotations and Double CNOTS."""
    self.remove_zero_rotations()
    while self.remove_double_cnots_once():
        pass


QuantumCircuit.optimize_gates = optimize_gates
CompositeGate.optimize_gates = optimize_gates


def remove_zero_rotations(self):
    """
    Remove Zero Rotations by looking (recursively) at rotation gates at the
    leaf ends.
    """
    # Removed at least one zero rotation.
    zero_rotation_removed = False
    new_data = []
    for gate in self.data:
        if isinstance(gate, CompositeGate):
            zero_rotation_removed |= gate.remove_zero_rotations()
            if gate.data:
                new_data.append(gate)
        else:
            if ((not isinstance(gate, Gate)) or
                    (not (gate.name == "rz" or gate.name == "ry" or
                          gate.name == "rx") or
                     (InitializeGate.chop_num(gate.params[0]) != 0))):
                new_data.append(gate)
            else:
                zero_rotation_removed = True

    self.data = new_data

    return zero_rotation_removed


QuantumCircuit.remove_zero_rotations = remove_zero_rotations
CompositeGate.remove_zero_rotations = remove_zero_rotations


def number_atomic_gates(self):
    """Count the number of leaf gates. """
    num = 0
    for gate in self.data:
        if isinstance(gate, CompositeGate):
            num += gate.number_atomic_gates()
        else:
            if isinstance(gate, Gate):
                num += 1
    return num


QuantumCircuit.number_atomic_gates = number_atomic_gates
CompositeGate.number_atomic_gates = number_atomic_gates


def remove_double_cnots_once(self):
    """
    Remove Double CNOTS paying attention that gates may be neighbours across
    Composite Gate boundaries.
    """
    num_high_level_gates = len(self.data)

    if num_high_level_gates == 0:
        return False
    else:
        if num_high_level_gates == 1 and isinstance(self.data[0],
                                                    CompositeGate):
            return self.data[0].remove_double_cnots_once()

    # Removed at least one double cnot.
    double_cnot_removed = False

    # last gate might be composite
    if isinstance(self.data[num_high_level_gates - 1], CompositeGate):
        double_cnot_removed = \
            double_cnot_removed or\
            self.data[num_high_level_gates - 1].remove_double_cnots_once()

    # don't start with last gate, using reversed so that can del on the go
    for i in reversed(range(num_high_level_gates - 1)):
        if isinstance(self.data[i], CompositeGate):
            double_cnot_removed =\
                double_cnot_removed \
                or self.data[i].remove_double_cnots_once()
            left_gate_host = self.data[i].last_atomic_gate_host()
            left_gate_index = -1
            # TODO: consider adding if semantics needed:
            # to remove empty composite gates
            # if left_gate_host == None: del self.data[i]
        else:
            left_gate_host = self.data
            left_gate_index = i

        if ((left_gate_host is not None) and
                left_gate_host[left_gate_index].name == "cx"):
            if isinstance(self.data[i + 1], CompositeGate):
                right_gate_host = self.data[i + 1].first_atomic_gate_host()
                right_gate_index = 0
            else:
                right_gate_host = self.data
                right_gate_index = i + 1

            if (right_gate_host is not None) \
                    and right_gate_host[right_gate_index].name == "cx" \
                    and (left_gate_host[left_gate_index].qargs ==
                         right_gate_host[right_gate_index].qargs):
                del right_gate_host[right_gate_index]
                del left_gate_host[left_gate_index]
                double_cnot_removed = True

    return double_cnot_removed


QuantumCircuit.remove_double_cnots_once = remove_double_cnots_once
CompositeGate.remove_double_cnots_once = remove_double_cnots_once


def first_atomic_gate_host(self):
    """Return the host list of the leaf gate on the left edge."""
    if self.data:
        if isinstance(self.data[0], CompositeGate):
            return self.data[0].first_atomic_gate_host()
        return self.data

    return None


QuantumCircuit.first_atomic_gate_host = first_atomic_gate_host
CompositeGate.first_atomic_gate_host = first_atomic_gate_host


def last_atomic_gate_host(self):
    """Return the host list of the leaf gate on the right edge."""
    if self.data:
        if isinstance(self.data[-1], CompositeGate):
            return self.data[-1].last_atomic_gate_host()
        return self.data

    return None


QuantumCircuit.last_atomic_gate_host = last_atomic_gate_host
CompositeGate.last_atomic_gate_host = last_atomic_gate_host


def initialize(self, params, qubits):
    """Apply initialize to circuit."""
    self._check_dups(qubits)
    for i in qubits:
        self._check_qubit(i)
        # TODO: make initialize an Instruction, and insert reset
        # TODO: avoid explicit reset if compiler determines a |0> state

    return self._attach(InitializeGate(params, qubits, self))


QuantumCircuit.initialize = initialize
CompositeGate.initialize = initialize


class GlobalPhaseGate(CompositeGate):
    
    """Simple Composite Gate that adjusts the global phase of the quantum state. Global phase, has no measurable significance, but it may be useful for automated simulation checking of the full statevector. 
    """
    
    def __init__(self, params, qargs, circ=None):
        """Create new Global Phase composite gate."""
        
        if len(qargs) == 0:
            raise QiskitError("Need at least one qubit.")

        if len(params) != 1:
            raise QiskitError("Global Phase takes a list of one and only one parameter.")

        # Check if phase is of unit amplitude
        if not math.isclose(np.absolute(params[0]), 1.0,
                            abs_tol=_EPS):
            raise QiskitError("Phase not of unit length")

        super().__init__("init", params, qargs, circ)

        phase = np.angle(params[0])
        self._attach(U3Gate(np.pi,phase,np.pi+phase,qargs[0]))
        self._attach(XGate(qargs[0]))

def globalphase_composite_gate(self, phase):
    """Apply GlobalPhaseGate to CompositeGate. Takes a single complex number of norm 1. """
    return self._attach(GlobalPhaseGate([phase], self.qargs))

def globalphase_circuit(self, phase):
    """Apply GlobalPhaseGate to circuit. Takes a single complex number of norm 1. """
    return self._attach(GlobalPhaseGate([phase], [(self.qregs[0],0)], self))


QuantumCircuit.globalphase = globalphase_circuit
CompositeGate.globalphase = globalphase_composite_gate
