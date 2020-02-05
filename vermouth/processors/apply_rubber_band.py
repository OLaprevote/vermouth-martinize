# Copyright 2018 University of Groningen
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Provides a processor that adds a rubber band elastic network.
"""
import itertools
import functools

import numpy as np
import networkx as nx

from .processor import Processor
from .. import selectors

DEFAULT_BOND_TYPE = 6


def self_distance_matrix(coordinates):
    """
    Compute a distance matrix between points in a selection.

    Notes
    -----
    This function does **not** account for periodic boundary conditions.

    Parameters
    ----------
    coordinates: numpy.ndarray
        Coordinates of the points in the selection. Each row must correspond
        to a point and each column to a dimension.

    Returns
    -------
    numpy.ndarray
    """
    return np.sqrt(
        np.sum(
            (coordinates[:, np.newaxis, :] - coordinates[np.newaxis, :, :]) ** 2,
            axis=-1)
    )


def compute_decay(distance, shift, rate, power):
    r"""
    Compute the decay function of the force constant as function to the distance.

    The decay function for the force constant is defined as:

    .. math::

        \exp^{-r(d - s)^p}

    where :math:`r` is the decay rate given by the 'rate' argument,
    :math:`p` is the decay power given by 'power', :math:`s` is a shift
    given by 'shift', and :math:`d` is the distance between the two atoms given
    in 'distance'. If the rate or the power are set to 0, then the decay
    function does not modify the force constant.

    The 'distance' argument can be a scalar or a numpy array. If it is an
    array, then the returned value is an array of decay factors with the same
    shape as the input.
    """
    return np.exp(-rate * ((distance - shift) ** power))


def compute_force_constants(distance_matrix, lower_bound, upper_bound,
                            decay_factor, decay_power, base_constant,
                            minimum_force):
    """
    Compute the force constant of an elastic network bond.

    The force constant can be modified with a decay function, and it can be
    bounded with a minimum threshold, or a distance upper and lower bonds.
    """
    constants = compute_decay(distance_matrix, lower_bound, decay_factor, decay_power)
    np.fill_diagonal(constants, 0)
    constants *= base_constant
    constants[constants < minimum_force] = 0
    constants[distance_matrix > upper_bound] = 0
    return constants


def are_connected(graph, left, right, separation):
    """
    ``True`` if the nodes are at most 'separation' nodes away.

    Parameters
    ----------
    graph: networkx.Graph
        The graph/molecule to work on.
    left:
        One node key from the graph.
    right:
        One node key from the graph.
    separation: int
        The maximum number of nodes in the shortest path between two nodes of
        interest for these two nodes to be considered connected. Must be >= 0.

    Returns
    -------
    bool
    """
    nodes_are_connected = False
    try:
        shortest_path = len(nx.shortest_path(graph, left, right))
    except nx.NetworkXNoPath:
        # There is no path between left and right so they are not
        # connected; which is the default.
        pass
    else:
        # The source and the target are counted in the shortest path
        nodes_are_connected = shortest_path <= separation + 2
    return nodes_are_connected


def build_connectivity_matrix(graph, separation, selection=None):
    """
    Build a connectivity matrix based on the separation between nodes in a graph.

    The connectivity matrix is a symmetric boolean matrix where cells contain
    ``True`` if the corresponding atoms are connected in the graph and
    separated by less or as much nodes as the given 'separation' argument.

    In the following examples, the separation between A and B is 0, 1, and 2.
    respectively:

    ```
    A - B
    A - X - B
    A - X - X - B
    ```

    Note that building the connectivity matrix with a separation of 0 is the
    same as building the adjacency matrix.

    Parameters
    ----------
    graph: networkx.Graph
        The graph/molecule to work on.
    separation: int
        The maximum number of nodes in the shortest path between two nodes of
        interest for these two nodes to be considered connected. Must be >= 0.
    selection: collections.abc.Collection
        A list of node keys to work on. If this argument is set, then the
        matrix is built only for the nodes in the selection; the whole graph is
        used to determine the paths. If set to `None` (default), then the matrix
        is built for all the nodes.

    Returns
    -------
    numpy.ndarray
        A boolean matrix.
    """
    if separation < 0:
        raise ValueError('Separation has to be null or positive.')
    if separation == 0:
        # The connectivity matrix with a separation of 0 is the adjacency
        # matrix. Thankfully, networkx can directly give it to us a a numpy
        # matrix.
        return np.asarray(nx.to_numpy_matrix(graph, nodelist=selection).astype(bool))
    if selection is None:
        selection = slice(None, None, None)
    size = graph.number_of_nodes()
    correspondence = {node: index for index, node in enumerate(graph.nodes)}
    connectivity = np.zeros((size, size), dtype=bool)
    # separation is provided in term of nodes while the path is provided in
    # terms of edges, hence `separation + 1`.
    distance_pairs = nx.all_pairs_shortest_path_length(graph, cutoff=separation + 1)
    for origin, target_and_distances in distance_pairs:
        for target in target_and_distances:
            connectivity[correspondence[origin], correspondence[target]] = True
    np.fill_diagonal(connectivity, False)
    return connectivity[:, selection][selection]


def build_pair_matrix(graph, criterion, selection):
    """
    Build a boolean matrix telling if a pair of nodes fulfil a criterion.

    Parameters
    ----------
    graph: networkx.Graph
        The graph/molecule to work on.
    criterion: Callable
        A function that determines if a pair of nodes fulfill the criterion.
        It takes a graph and two node keys as arguments and returns a boolean.
    selection: collections.abc.Collection
        A list of node keys to work on. If this argument is set, then the
        matrix is built only for the nodes in the selection. If set to
        `None` (default), then the matrix is built for all the nodes.

    Returns
    -------
    numpy.ndarray
        A boolean matrix.
    """
    if selection is None:
        size = len(graph)
        selected_nodes = graph.nodes
    else:
        size = len(selection)
        selected_nodes = (node for node in graph.nodes if node in selection)
    share_domain = np.zeros((size, size), dtype=bool)
    node_combinations = itertools.combinations(enumerate(selected_nodes), 2)
    for (idx, key_idx), (jdx, key_jdx) in node_combinations:
        share_domain[idx, jdx] = criterion(graph, key_idx, key_jdx)
        share_domain[jdx, idx] = share_domain[idx, jdx]
    return share_domain


def apply_rubber_band(molecule, selector,
                      lower_bound, upper_bound,
                      decay_factor, decay_power,
                      base_constant, minimum_force,
                      bond_type, domain_criterion, res_min_dist=3):
    r"""
    Adds a rubber band elastic network to a molecule.

    The elastic network is applied as bounds between the atoms selected by the
    function declared with the 'selector' argument. The equilibrium length for
    the bonds is measured from the coordinates in the molecule, the force
    constant is computed from the base force constant and an optional decay
    function.

    The decay function for the force constant is defined as:

    .. math::

        \exp^{-r(d - s)^p}

    where :math:`r` is the decay rate given by the 'decay_factor' argument,
    :math:`p` is the decay power given by 'decay_power', :math:`s` is a shift
    given by 'lower_bound', and :math:`d` is the distance between the two atoms
    in the molecule. If the rate or the power are set to 0, then the decay
    function does not modify the force constant.

    The 'selector' argument takes a callback that accepts a atom dictionary and
    returns ``True`` if the atom match the conditions to be kept.

    Only nodes that are in the same domain can be connected by the elastic
    network. The 'domain_criterion' argument accepts a callback that determines
    if two nodes are in the same domain. That callbacks accepts a graph and two
    node keys as argument and returns whether or not the nodes are in the same
    domain as a boolean.

    Parameters
    ----------
    molecule: Molecule
        The molecule to which apply the elastic network. The molecule is
        modified in-place.
    selector: collections.abc.Callable
        Selection function.
    lower_bound: float
        The minimum length for a bond to be added, expressed in
        nanometers.
    upper_bound: float
        The maximum length for a bond to be added, expressed in
        nanometers.
    decay_factor: float
        Parameter for the decay function.
    decay_power: float
        Parameter for the decay function.
    base_constant: float
        The base force constant for the bonds in :math:`kJ.mol^{-1}.nm^{-2}`.
        If 'decay_factor' or 'decay_power' is set to 0, then it will be the
        used force constant.
    minimum_force: float
        Minimum force constant in :math:`kJ.mol^{-1}.nm^{-2}` under which bonds
        are not kept.
    bond_type: int
        Gromacs bond function type to apply to the elastic network bonds.
    domain_criterion: Callback
        Function to establish if two atoms are part of the same domain. Elastic
        bonds are only added within a domain. By default, all the atoms in
        the molecule are considered part of the same domain. The function
        expects a graph (e.g. a :class:`Molecule`) and two atom node keys as
        argument and returns ``True`` if the two atoms are part of the same
        domain; returns ``False`` otherwise.
    res_min_dist: int
        Minimum separation between two atoms for a bond to be kept.
        Bonds are kept is the separation is greater or equal to the value
        given.
    """
    selection = []
    coordinates = []
    missing = []
    for node_key, attributes in molecule.nodes.items():
        if selector(attributes):
            selection.append(node_key)
            coordinates.append(attributes.get('position'))
            if coordinates[-1] is None:
                missing.append(node_key)
    if missing:
        raise ValueError('All atoms from the selection must have coordinates. '
                         'The following atoms do not have some: {}.'
                         .format(' '.join(missing)))
    coordinates = np.stack(coordinates)
    distance_matrix = self_distance_matrix(coordinates)
    constants = compute_force_constants(distance_matrix, lower_bound,
                                        upper_bound, decay_factor, decay_power,
                                        base_constant, minimum_force)
    connected = build_connectivity_matrix(molecule, res_min_dist - 1,
                                          selection=selection)
    same_domain = build_pair_matrix(molecule, domain_criterion,
                                    selection=selection)
    can_be_linked = (~connected) & same_domain
    # Multiply the force constant by 0 if the nodes cannot be linked.
    constants *= can_be_linked
    distance_matrix = distance_matrix.round(5)  # For compatibility with legacy
    for from_idx, to_idx in zip(*np.triu_indices_from(constants)):
        from_key = selection[from_idx]
        to_key = selection[to_idx]
        force_constant = constants[from_idx, to_idx]
        length = distance_matrix[from_idx, to_idx]
        if force_constant > minimum_force:
            molecule.add_interaction(
                type_='bonds',
                atoms=(from_key, to_key),
                parameters=[bond_type, length, force_constant],
                meta={'group': 'Rubber band'},
            )


def always_true(*args, **kwargs):
    """
    Returns ``True`` whatever the arguments are.
    """
    return True


def same_chain(graph, left, right):
    """
    Returns ``True`` is the nodes are part of the same chain.

    Nodes are considered part of the same chain if they both have the same value
    under the "chain" attribute, or if none of the 2 nodes have that attribute.

    Parameters
    ----------
    graph: networkx.Graph
        A graph the nodes are part of.
    left:
        A node key in 'graph'.
    right:
        A node key in 'graph'.

    Returns
    -------
    bool
        ``True`` if the nodes are part of the same chain.
    """
    node_left = graph.nodes[left]
    node_right = graph.nodes[right]
    return node_left.get('chain') == node_right.get('chain')


class ApplyRubberBand(Processor):
    def __init__(self, lower_bound, upper_bound, decay_factor, decay_power,
                 base_constant, minimum_force,
                 bond_type=None,
                 selector=selectors.select_backbone,
                 bond_type_variable='elastic_network_bond_type',
                 domain_criterion=always_true):
        super().__init__()
        self.lower_bound = lower_bound
        self.upper_bound = upper_bound
        self.decay_factor = decay_factor
        self.decay_power = decay_power
        self.base_constant = base_constant
        self.minimum_force = minimum_force
        self.bond_type = bond_type
        self.selector = selector
        self.bond_type_variable = bond_type_variable
        self.domain_criterion = domain_criterion

    def run_molecule(self, molecule):
        # Choose the bond type. From high to low, the priority order is:
        # * what is set as an argument to the processor
        # * what is written in the force field variables
        #   under the key `self.bond_type_variable`
        # * the default value set in DEFAULT_BOND_TYPE
        bond_type = self.bond_type
        if self.bond_type is None:
            bond_type = molecule.force_field.variables.get(self.bond_type_variable,
                                                           DEFAULT_BOND_TYPE)

        apply_rubber_band(molecule, self.selector,
                          lower_bound=self.lower_bound,
                          upper_bound=self.upper_bound,
                          decay_factor=self.decay_factor,
                          decay_power=self.decay_power,
                          base_constant=self.base_constant,
                          minimum_force=self.minimum_force,
                          bond_type=bond_type,
                          domain_criterion=self.domain_criterion)
        return molecule
