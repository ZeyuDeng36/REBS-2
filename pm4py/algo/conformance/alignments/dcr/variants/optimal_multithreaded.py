"""
This module contains the object-oriented implementation of the Optimal Alignments algorithm,
based on the paper by Axel Kjeld Fjelrad Christfort and Tijs Slaats [1].

Overview:
The implementation encapsulates the core components of the algorithm and provides.

The calculation of the alignments and graph-trace handling are encapsulated in separate,
dedicated classes, thereby facilitating modularity and reuse.
Central to the module are the following classes:

- `LogAlignment`: A simplified interface to perform optimal alignment through the other classes.
- `TraceAlignment`: Serves as the primary interface for interacting with the algorithm.
  orchestrating the alignment process and providing access to performance metrics.
- `TraceHandler`: Manages the conversion and handling of event traces, preparing them for alignment.
- `DCRGraphHandler`: Encapsulates operations and checks on DCR graphs relevant for the alignment.
- `Alignment`: Implements the actual algorithm, managing the search space, and constructing optimal alignments.

The module's classes interact to process an input DCR graph and a trace abd execute the alignment algorithm. This process helps in understanding how closely the behavior described by
the trace matches the behavior allowed by the DCR graph, which is essential in the analysis and optimization of business processes.

This version of the module includes multithreading capabilities to improve performance when processing large logs.

References
----------
.. [1]
    A. K. F. Christfort, T. Slaats, "Efficient Optimal Alignment Between Dynamic Condition Response Graphs and Traces",
    in Business Process Management, Springer International Publishing, 2023, pp. 3-19.
    DOI <https://doi.org/10.1007/978-3-031-41620-0_1>_.
"""

import concurrent.futures
import logging
import time
import pandas as pd
import multiprocessing
from copy import deepcopy, copy
from typing import Optional, Dict, Any, Union, List, Tuple
from heapq import heappop, heappush
from enum import Enum

from pm4py.objects.dcr.obj import DcrGraph
from pm4py.objects.dcr.semantics import DcrSemantics
from pm4py.util import constants, xes_constants, exec_utils
from pm4py.objects.log.obj import EventLog, Trace
from pm4py.objects.conversion.log import converter as log_converter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class LogAlignment:
    """
    LogAlignment class provides a multithreaded interface to perform optimal alignment for multiple traces in an event log.

    This class manages the parallel processing of traces, distributing the workload across multiple CPU cores
    to improve performance when dealing with large event logs.

    Attributes:
        parameters (Dict[str, Any]): Configuration parameters for the alignment process.
        cpu_count (int): The number of CPU cores available on the system.
        max_workers (int): The maximum number of worker processes to use for parallel processing.
        chunk_size (int): The number of traces to process in each chunk.
        traces (EventLog): The event log containing the traces to be aligned.

    Methods:
        perform_log_alignment(graph: DcrGraph, parameters: Dict[str, Any] = None) -> List[Dict[str, Any]]:
            Performs the alignment process for all traces in the log using multiple worker processes.

        process_chunk(graph: DcrGraph, traces: List[Trace], parameters: Dict[str, Any]) -> List[Dict[str, Any]]:
            Static method to process a chunk of traces, used by worker processes.
    """
    def __init__(self, traces: EventLog, parameters: Dict[str, Any] = None):
        self.parameters = parameters or {}
        self.cpu_count = multiprocessing.cpu_count()
        self.max_workers = self.parameters.get("max_workers", max(1, self.cpu_count - 1))
        self.chunk_size = self.parameters.get("chunk_size", max(1, min(100, len(traces) // (self.cpu_count * 2))))
        self.traces = traces
        logger.info(f"LogAlignment initialized with {len(self.traces)} traces")
        logger.info(f"System has {self.cpu_count} CPU cores")
        logger.info(f"Using {self.max_workers} worker processes, chunk size of {self.chunk_size}")

    def perform_log_alignment(self, graph: DcrGraph, parameters: Dict[str, Any] = None) -> List[Dict[str, Any]]:
        """
        Performs the alignment process for all traces in the log using multiple worker processes.

        This method divides the traces into chunks and distributes them among worker processes
        for parallel processing. It then collects and combines the results from all workers.

        Parameters:
            graph (DcrGraph): The DCR graph against which the traces will be aligned.
            parameters (Dict[str, Any], optional): Additional parameters for the alignment process.

        Returns:
            List[Dict[str, Any]]: A list of alignment results for all processed traces.
        """
        if not self.traces:
            logger.warning("No valid traces to align.")
            return []

        all_results = []
        start_time = time.time()

        with concurrent.futures.ProcessPoolExecutor(max_workers=self.max_workers) as executor:
            futures = []
            for i in range(0, len(self.traces), self.chunk_size):
                chunk = self.traces[i:i + self.chunk_size]
                futures.append(executor.submit(self.process_chunk, graph, chunk, parameters))

            for future in concurrent.futures.as_completed(futures):
                try:
                    results = future.result()
                    all_results.extend(results)
                except Exception as e:
                    logger.error(f"Error processing chunk: {str(e)}")

        logger.info(
            f"Alignment completed in {time.time() - start_time:.2f} seconds. {len(all_results)} traces aligned.")
        return all_results

    @staticmethod
    def process_chunk(graph: DcrGraph, traces: List[Trace], parameters: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Static method to process a chunk of traces.

        This method is designed to be run in a separate process, aligning each trace in the given chunk
        against the provided DCR graph.

        Parameters:
            graph (DcrGraph): The DCR graph against which the traces will be aligned.
            traces (List[Trace]): A list of traces to be processed.
            parameters (Dict[str, Any]): Parameters for the alignment process.

        Returns:
            List[Dict[str, Any]]: A list of alignment results for the processed traces.
        """
        results = []
        for trace in traces:
            alignment = TraceAlignment(graph, trace, parameters=parameters)
            results.extend(alignment.perform_alignment())
        return results


class TraceAlignment:
    """
    The TraceAlignment class provides a simplified interface to perform optimal alignment for DCR graphs,
    abstracting the complexity of direct interactions with the DCRGraphHandler, TraceHandler, and Alignment classes.

    Users should initialize TraceAlignment with a DCR_Graph object and a trace object, which can be a list of events, a Pandas DataFrame,
    an EventLog, or a Trace. Optional parameters can also be passed to customize the processing, such as specifying custom activity
    and case ID keys.

    After initializing TraceAlignment, users can call perform_alignment() to execute the alignment process, which returns the result of
    the alignment procedure.

    Example usage:
        Define your instances of DCR graph and trace representation as 'graph' and 'trace'
        facade = Facade(graph, trace)
        alignment_result = facade.perform_alignment()

    Note:
    - The user is expected to have a basic understanding of DCR graphs and trace alignment in the context of process mining.

    Attributes:
        graph_handler (DCRGraphHandler): Handler for DCR graph operations.
        trace_handler (TraceHandler): Handler for trace operations.
        alignment (Alignment): Instance that holds the result of the alignment process, initialized to None.

    Methods:
        perform_alignment(): Performs trace alignment against the DCR graph and returns the alignment result.
        get_performance_metrics(): Calculates and returns the alignment fitness.
    """

    def __init__(self, graph: DcrGraph, trace: Union[List[Tuple[str]], pd.DataFrame, EventLog, Trace],
                 parameters: Optional[Dict] = None):
        """
        Initializes the facade with a DCR graph and a trace to be processed.

        The facade serves as a simplified interface to perform alignment between
        the provided DCR graph and the trace, handling the creation and coordination
        of the necessary handler objects.

        Parameters
        ----------
        graph : DcrGraph
            The DCR graph against which the trace will be aligned. The graph should
            encapsulate the behavior model.
        trace : Union[List[Dict[str, Any]], pd.DataFrame, EventLog, Trace]
            The trace to be aligned with the DCR graph. The trace can be in various
            forms, such as a list of dictionaries representing events, a pandas
            DataFrame, an EventLog object, or a Trace object.
        parameters : Optional[Dict], optional
            A dictionary of parameters that can be used to fine-tune the handling
            of the graph and trace. The exact parameters that can be provided will
            depend on the implementation of the DCRGraphHandler and TraceHandler.

        Attributes
        ----------
        self.graph_handler : DCRGraphHandler
            An instance of DCRGraphHandler to manage operations related to the DCR graph.
        self.trace_handler : TraceHandler
            An instance of TraceHandler to manage the conversion and processing of the trace.
        self.alignment : Alignment or None
            An instance of the Alignment class that will be initialized after
            perform_alignment is called. This will hold the result of the trace
            alignment against the DCR graph.
        """
        self.graph_handler = DCRGraphHandler(graph)
        self.trace_handler = TraceHandler(trace, parameters)
        self.alignment = None  # This will hold an instance of Alignment class after perform_alignment is called
        self.result = None

    def perform_alignment(self):
        if not self.trace_handler.trace:
            print(f"Warning: The trace is empty. Skipping alignment.")
            return []

        try:
            self.alignment = Alignment(self.graph_handler, self.trace_handler)
            self.result = self.alignment.apply_trace()

            if not self.result:
                print("Warning: No alignment result produced.")
                return []

            self.get_performance_metrics()
            return [self.result]
        except Exception as e:
            print(f"Error during alignment: {str(e)}")
            return []

    def get_performance_metrics(self):
        # Ensure that alignment has been performed before calculating performance metrics
        # Calculate and return fitness and precision based on the alignment result
        performance = Performance(self.alignment, self.graph_handler, self.trace_handler)
        fitness_bwc = performance.calculate_fitness()
        self.result[Outputs.ALIGN_FITNESS.value] = fitness_bwc[0]
        self.result[Outputs.BEST_WORST_COST.value] = fitness_bwc[1]



class Parameters(Enum):
    """
    Enumeration that defines keys and constants for various parameters used in the alignment process.

    Attributes:
        CASE_ID_KEY: The key used to identify the case ID in the event log.
        ACTIVITY_KEY: The key used to identify the activity in the event log.
        SYNC_COST: The cost of a synchronous move during the alignment.
        MODEL_COST: The cost of a model move during the alignment.
        LOG_COST: The cost of a log move during the alignment.
    """
    CASE_ID_KEY = constants.PARAMETER_CONSTANT_CASEID_KEY
    ACTIVITY_KEY = constants.PARAMETER_CONSTANT_ACTIVITY_KEY
    SYNC_COST = 0
    MODEL_COST = 1
    LOG_COST = 1


class Outputs(Enum):
    """
    Enumeration that defines constants for various outputs of the alignment process.

    Attributes:
        ALIGNMENT: The key for accessing the final alignment from the result.
        COST: The key for accessing the total cost of the alignment.
        VISITED: The key for accessing the number of visited states during the alignment process.
        CLOSED: The key for accessing the number of closed states during the alignment process.
        GLOBAL_MIN: The key for accessing the global minimum cost encountered during the alignment.
        MODEL_MOVE_FITNESS = the key for accessing the model move fitness
        LOG_MOVE_FITNESS = the key for accessing the log move fitness
        ALIGN_FITNESS = the key for accessing the alignment fitness
    """
    ALIGNMENT = "alignment"
    COST = "cost"
    VISITED = "visited_states"
    CLOSED = "closed"
    GLOBAL_MIN = "global_min"
    ALIGN_FITNESS = 'fitness'
    BEST_WORST_COST = "bwc"

class Performance:
    def __init__(self, alignment, graph_handler, trace_handler):
        self.alignment = alignment
        self.graph_hanlder = graph_handler
        self.trace_handler = trace_handler

    def calculate_fitness(self) -> Tuple:
        """
        From the Conformance Checking book [1].
        Calculate the fitness of the alignment based on the optimal and worst-case costs.

        The fitness is calculated as one minus the ratio of the optimal alignment cost to
        the worst-case alignment cost. If the worst-case alignment cost is zero,
        fitness is set to zero to avoid division by zero.

        Returns
        -------
        float
            The calculated fitness value, where higher values indicate a better fit.

        References
        ----------
        * [1] C. Josep et al., "Conformance Checking Software",  Springer International Publishing, 82-91, 2018. `DOI <https://doi.org/10.1007/978-3-319-99414-7>`_.
        """
        # run model with empty trace
        worst_case_trace = len(self.trace_handler.trace)
        self.trace_handler.trace = ()
        # compute worst_best_alignment
        best_worst_alignment = Alignment(self.graph_hanlder, self.trace_handler)
        best_worst_result = best_worst_alignment.apply_trace()
        bwc = (worst_case_trace + best_worst_result[Outputs.COST.value])
        fitness = 1 - (self.alignment.global_min / (bwc) if bwc > 0 else 0)
        return fitness, bwc


class TraceHandler:
    """
    TraceHandler is responsible for managing and converting traces into a format suitable
    for the alignment algorithm. This class provides functionalities to check if the trace is
    empty, retrieve the first activity from the trace, and convert the trace format as needed.

    A trace can be provided as a list of dictionaries, a pandas DataFrame, an EventLog, or a Trace object.
    The TraceHandler takes care of converting these into a uniform internal representation.

    Attributes
    ----------
    trace : Tuple[Any] | Trace
        The trace to be managed and converted. It's stored internally in a list of dictionaries
        regardless of the input format.
    activity_key : str
        The key to identify activities within the trace data.

    Methods
    -------
    is_empty() -> bool:
        Checks if the trace is empty (contains no events).

    get_first_activity() -> Any:
        Retrieves the first activity from the trace, if available.

    convert_trace(activity_key, case_id_key, parameters):
        Converts the trace into a tuple-based format required for processing by the alignment algorithm.
        This conversion handles both DataFrame and Event Log traces and can be configured via parameters.

    Parameters
    ----------
    trace : Tuple[str] | Trace
        The initial trace data provided in one of the acceptable formats.
    parameters : Optional[Dict]
        Optional parameters for trace conversion. These can define the keys for activity and case ID within
        the trace data and can include other conversion-related parameters.
    """

    def __init__(self, trace: Union[Tuple[str], Trace],
                 parameters: Optional[Dict] = None):
        """
        Initializes the TraceHandler object, converting the input trace into a standard format
        and storing the specified parameters.

        The conversion process varies depending on the type of trace input provided. The trace is
        converted to a list of dictionary records for consistent internal processing.

        Parameters
        ----------
        trace : Tuple[str] | Trace
            The initial trace data provided in one of: Pandas DataFrame, an EventLog, or a single Trace.
        parameters : Optional[Dict]
            Optional parameters for trace conversion. These can define the keys for activity and case ID within
            the trace data and can include other conversion-related parameters. If None or not a dictionary,
            defaults will be used.

        The activity key used in the trace is determined by the provided parameters or defaults to
        a standard key from the xes_constants if not specified.
        """
        if parameters is None or not isinstance(parameters, dict):
            parameters = {}

        self.activity_key = parameters.get(Parameters.ACTIVITY_KEY.value, xes_constants.DEFAULT_NAME_KEY)

        if isinstance(trace, Trace):
            self.trace = tuple(event[self.activity_key] for event in trace)
        else:
            # Ensure trace is properly initialized
            if trace and isinstance(trace, tuple) and all(isinstance(act, str) for act in trace):
                self.trace = trace
            else:
                self.trace = tuple()  # Initialize as an empty tuple instead of None

    def is_empty(self) -> bool:
        return not bool(self.trace)

    def get_first_activity(self) -> Any:
        return self.trace[0] if self.trace else None


class DCRGraphHandler:
    """
    DCRGraphHandler manages operations on a DCR graph within the context of an alignment algorithm.
    It provides methods to check if an event is enabled, if the graph is in an accepting state,
    and to execute an event on the graph.

    The DCR graph follows the semantics defined in the DCR semantics module, and this class
    acts as an interface to apply these semantics for the purpose of alignment computation.

    Attributes
    ----------
    graph : DcrGraph
        The DCR graph on which the operations are to be performed.

    Methods
    -------
    is_enabled(event: Any) -> bool:
        Determines if an event is enabled in the current state of the DCR graph.

    is_accepting() -> bool:
        Checks if the current state of the DCR graph is an accepting state.

    execute(event: Any, curr_graph) -> Any:
        Executes an event on the DCR graph, which may result in a transition to a new state.
        If the execution is not possible, it returns the current graph state.

    Parameters
    ----------
    graph : DcrGraph
        An instance of a DCR_Graph object which the handler will manage and manipulate.

    Raises
    ------
    TypeError
        If the provided graph is not an instance of DCR_Graph.
    """

    def __init__(self, graph: DcrGraph):
        if not isinstance(graph, DcrGraph):
            raise TypeError(f"Expected a DCR_Graph object, got {type(graph)} instead")
        self.graph = graph

    def is_enabled(self, event: Any) -> bool:
        return DcrSemantics.is_enabled(event, self.graph)

    def enabled(self):
        return DcrSemantics.enabled(self.graph)

    def is_accepting(self) -> bool:
        return DcrSemantics.is_accepting(self.graph)

    def execute(self, event: Any, curr_graph) -> Any:
        new_graph = DcrSemantics.execute(curr_graph, event)
        if not new_graph:
            return curr_graph
        return new_graph

class Alignment:
    def __init__(self, graph_handler: DCRGraphHandler, trace_handler: TraceHandler, parameters: Optional[Dict] = None):
        """
        Initialize the Alignment instance.
        This constructor initializes the alignment with the provided DCR graph and trace handlers. It sets up
        all necessary data structures for computing the alignment and its costs.

        Parameters
        ----------
        graph_handler : DCRGraphHandler
            An instance of DCRGraphHandler to manage the DCR graph.
        trace_handler : TraceHandler
            An instance of TraceHandler to manage the event log trace.
        parameters : Optional[Dict]
            A dictionary of parameters to configure the alignment process. Defaults are used if None.
        """
        self.graph_handler = graph_handler

        if parameters is None:
            parameters = {}
        activity_key = exec_utils.get_param_value(Parameters.ACTIVITY_KEY, parameters, xes_constants.DEFAULT_NAME_KEY)
        parameters[Parameters.ACTIVITY_KEY.value] = activity_key

        self.trace_handler = TraceHandler(trace_handler.trace, parameters)

        self.open_set = []
        self.max_cost = 0
        self.global_min = float('inf')
        self.closed_set = {}
        self.visited_states = set()
        self.new_moves = []
        self.final_alignment = []
        self.initial_marking = deepcopy(self.graph_handler.graph.marking)

    def handle_state(self, curr_cost, curr_graph, curr_trace, event, moves, move_type=None):
        """
        Manages the transition to a new state in the alignment algorithm based on the specified move type.
        It computes the new state, checks for execution equivalency to avoid re-processing, and if unique,
        updates the visited states and the priority queue for further processing.

        Parameters
        ----------
        curr_cost : int
            The current cost of the alignment.
        curr_graph : DcrGraph
            The current state of the DCR graph.
        curr_trace : list
            The current state of the trace.
        event : Any
                The event from the trace that is being considered in the current alignment step.
        moves : list
            The list of moves made so far.
        move_type : str, optional
            The type of move to make. This should be one of "sync", "model", or "log". Default is None.

        Returns
        -------
        None

        Raises
        ------
        None

        Notes
        -----
        - This method interfaces with the `get_new_state` method to compute the new state.
        - It employs a heap-based priority queue to manage the processing order of states based on their costs.
        - Execution equivalency check is performed to reduce redundant processing of similar states.
        """
        if moves is None:
            moves = []

        new_cost, new_graph, new_trace, new_move = self.get_new_state(curr_cost, curr_graph, curr_trace, event,
                                                                      move_type)

        state_representation = (str(new_graph), tuple(map(str, new_trace)))
        if state_representation not in self.visited_states:
            self.visited_states.add(state_representation)
            new_moves = moves + [new_move]
            heappush(self.open_set,(new_cost, new_graph, new_trace, str(new_graph), new_moves))

    def get_new_state(self, curr_cost, curr_marking, curr_trace, event, move_type):
        """
        Computes the new state of the alignment algorithm based on the current state and
        the specified move type. The new state includes the updated cost, graph, trace,
        and move. This method handles three types of moves: synchronous, model, and log.

        Parameters
        ----------
        curr_cost : int
            The current cost of the alignment.
        curr_graph : DcrGraph
            The current state of the DCR graph.
        curr_trace : list
            The current state of the trace.
        moves : list
            The list of moves made so far.
        move_type : str
            The type of move to make. This should be one of "sync", "model", or "log".

        Returns
        -------
        tuple
            A tuple containing four elements:
            - new_cost : int, the updated cost of the alignment.
            - new_graph : DCR_Graph, the updated state of the DCR graph.
            - new_trace : list, the updated state of the trace.
            - new_move : tuple, a tuple representing the move made, formatted as (move_type, first_activity).

        Example
        -------
        new_cost, new_graph, new_trace, new_move = get_new_state(curr_cost, curr_graph, curr_trace, moves, "sync")

        """
        new_cost = curr_cost
        self.graph_handler.graph.marking = deepcopy(curr_marking)
        new_graph = self.graph_handler.graph
        new_trace = curr_trace
        new_move = None
        if move_type == "sync":
            new_cost += Parameters.SYNC_COST.value
            new_move = (event, event)
            new_trace = curr_trace[1:]
            new_graph = self.graph_handler.execute(event, new_graph)

        elif move_type == "model":
            new_cost += Parameters.MODEL_COST.value
            new_move = (event, ">>")
            new_graph = self.graph_handler.execute(event, new_graph)

        elif move_type == "log":
            new_cost += Parameters.LOG_COST.value
            new_move = (">>", event)
            new_trace = curr_trace[1:]

        return new_cost, deepcopy(new_graph.marking), new_trace, new_move

    def update_closed_and_visited_sets(self, curr_cost, state_repr):
        self.closed_set[state_repr] = curr_cost

    def process_current_state(self, current):
        """
        Process the current state in the alignment process.
        This method processes the current state of the alignment, updates the graph handler
        with the current graph, and prepares the state representation for further processing.

        Parameters
        ----------
        current : Tuple
            The current state, which is a tuple containing the current cost, current graph,
            current trace, and the moves made up to this point.
        """
        curr_cost, curr_marking, curr_trace, _, moves = current
        self.graph_handler.graph.marking = deepcopy(curr_marking)

        # Check if the trace is None or empty and handle accordingly
        if curr_trace is None or not curr_trace:
            return curr_cost, curr_marking, (), str(self.graph_handler.graph.marking), moves

        state_repr = (str(self.graph_handler.graph.marking), tuple(map(str, curr_trace)))
        return curr_cost, curr_marking, curr_trace, state_repr, moves

    def check_accepting_conditions(self, curr_cost, is_accepting):
        """
        Check if the current state meets the accepting conditions and update the global minimum cost and final alignment.

        Parameters
        ----------
        curr_cost : float
            The cost associated with the current state.
        is_accepting : bool
            Flag indicating whether the current state is an accepting state.

        Returns
        -------
        float
            The final cost if the accepting conditions are met; `float('inf')` otherwise.
        """
        if is_accepting:
            if curr_cost <= self.global_min:
                self.global_min = curr_cost
                self.final_alignment = self.new_moves

        return self.global_min


    def apply_trace(self, parameters=None):
        """
        Applies the alignment algorithm to a trace in order to find an optimal alignment
        between the DCR graph and the trace based on the algorithm outlined in the paper
        by Axel Kjeld Fjelrad Christfort and Tijs Slaats.
        https://link.springer.com/chapter/10.1007/978-3-031-41620-0_1

        Parameters
        ----------
        parameters : dict, optional
            A dictionary of parameters to configure the alignment algorithm.
            Possible keys include:
            - Parameters.ACTIVITY_KEY: Specifies the key to use for activity names in the trace data.
            - Parameters.CASE_ID_KEY: Specifies the key to use for case IDs in the trace data.
            If not provided or None, default values are used.

        Returns
        -------
        dict
            A dictionary containing the results of the alignment algorithm, with the following keys:
            - 'alignment': List of tuples representing the optimal alignment found.
            - 'cost': The cost of the optimal alignment.
            - 'visited': The number of states visited during the alignment algorithm.
            - 'closed': The number of closed states during the alignment algorithm.
            - 'global_min': The global minimum cost found during the alignment algorithm.

        Example
        -------
        result = alignment_obj.apply_trace()
        optimal_alignment = result['alignment']
        alignment_cost = result['cost']
        """

        visited, closed, cost, self.final_alignment, final_cost = 0, 0, 0, None, float('inf')
        self.open_set.append(
            (cost, deepcopy(self.graph_handler.graph.marking), self.trace_handler.trace,
             str(self.graph_handler.graph.marking), []))

        while self.open_set:
            current = heappop(self.open_set)
            visited += 1
            result = self.process_current_state(current)
            if result is None:
                continue
            curr_cost, curr_trace, state_repr, moves = result[0], result[2], result[3], result[4]
            if curr_cost > final_cost:
                continue
            self.update_closed_and_visited_sets(curr_cost, state_repr)
            closed += 1
            self.trace_handler.trace = curr_trace
            if self.graph_handler.is_accepting() and self.trace_handler.is_empty():
                self.new_moves = moves
                final_cost = self.check_accepting_conditions(curr_cost, self.graph_handler.is_accepting())
                self.max_cost = final_cost

            try:
                self.perform_moves(curr_cost, current, moves)
            except Exception as e:
                print(f"Error performing moves: {str(e)}")
                continue

        if self.final_alignment is None:
            print("Warning: No valid alignment found.")
            return None

        return self.construct_results(visited, closed, final_cost)

    def skip_current(self, result):
        #if state is visited, and cost is the same skip
        curr_cost, state_repr = result[0], result[3]
        visitCost = self.closed_set.get(state_repr,float("inf"))
        return visitCost <= curr_cost and visitCost is not float("inf")

    def perform_moves(self, curr_cost, current, moves):
        """
        Defines available moves based on the trace and model state.

        This method determines which moves are possible (synchronous, model, or log moves)
        so that they can be processed accordingly. Each move type is defined as:
        - Synchronous (sync): The activity is both in the trace and the model, and is currently enabled.
        - Model move: The activity is enabled in the model but not in the trace.
        - Log move: The activity is in the trace but not enabled in the model.

        Parameters
        ----------
        curr_cost : float
            The cost associated with the current state before performing any moves.
        current : tuple
            The current state represented as a tuple containing the following:
            - current[0]: The current cumulative cost associated with the trace.
            - current[1]: The current state of the graph representing the model.
            - current[2]: The current position in the trace.
            - current[3]: A placeholder for additional information (if any).
            - current[4]: The list of moves performed to reach this state.
        moves : list
            The list of moves performed so far to reach the current state. This will be updated with new moves
            as they are performed.
        """
        self.graph_handler.graph.marking = current[1]
        first_activity_name = self.trace_handler.get_first_activity()

        if first_activity_name is None:
            return  # No more activities in the trace, just return

        try:
            first_activity = self.graph_handler.graph.get_event(first_activity_name)
        except KeyError:
            self.handle_state(curr_cost, current[1], current[2], first_activity_name, moves, "log")
            return

        enabled = self.graph_handler.enabled()
        is_enabled = self.graph_handler.is_enabled(first_activity)

        if is_enabled:
            self.handle_state(curr_cost, current[1], current[2], first_activity, moves, "sync")
            return

        self.handle_state(curr_cost, current[1], current[2], first_activity, moves, "log")

        for event in enabled:
            self.handle_state(curr_cost, current[1], current[2], event, moves, "model")

    def construct_results(self, visited, closed, final_cost):
        """
        Constructs a dictionary of results from the alignment process containing various metrics
        and outcomes, such as the final alignment, its cost, and statistics about the search process.

        Parameters
        ----------
        visited : int
            The number of states visited during the alignment process.
        closed : int
            The number of states that were closed (i.e., fully processed and will not be revisited).
        final_cost : float
            The cost associated with the final alignment obtained.

        Returns
        -------
        dict
            A dictionary with keys corresponding to various outputs of the alignment process:
            - 'alignment': The final alignment between the process model and the trace.
            - 'cost': The cost of the final alignment   .
            - 'visited': The total number of visited states.
            - 'closed': The total number of closed states.
            - 'model move fitness': the fitness provided that model moves are used
            - 'log move fitness': the fitness provided by the log moves
        """
        self.graph_handler.graph.marking = self.initial_marking
        return {
            Outputs.ALIGNMENT.value: self.final_alignment,
            Outputs.COST.value: final_cost,
            Outputs.VISITED.value: visited,
            Outputs.CLOSED.value: closed,
            Outputs.GLOBAL_MIN.value: self.global_min,
        }


def apply(trace_or_log: Union[pd.DataFrame, EventLog, Trace], graph: DcrGraph, parameters=None):
    """
    Applies the alignment algorithm to either a single trace or an entire event log.

    This function serves as the main entry point for the alignment process. It determines
    whether to use the single-trace alignment or the multi-threaded log alignment based on
    the input type and size.

    Parameters:
        trace_or_log (Union[pd.DataFrame, EventLog, Trace]): The input trace or log to be aligned.
        graph (DcrGraph): The DCR graph against which the alignment will be performed.
        parameters (dict, optional): Additional parameters for the alignment process.

    Returns:
        Union[List[Dict[str, Any]], Dict[str, Any]]: The alignment results. For a single trace,
        it returns a dictionary. For a log, it returns a list of dictionaries.
    """
    if isinstance(trace_or_log, Trace):
        alignment = TraceAlignment(graph, trace_or_log, parameters=parameters)
        return alignment.perform_alignment()
    else:
        if isinstance(trace_or_log, pd.DataFrame):
            trace_or_log = log_converter.apply(trace_or_log, variant=log_converter.Variants.TO_EVENT_LOG)

        if len(trace_or_log) < 100:
            return apply_original(trace_or_log, graph, parameters)

        alignment = LogAlignment(trace_or_log, parameters=parameters)
        return alignment.perform_log_alignment(graph, parameters=parameters)


def apply_original(log: EventLog, graph: DcrGraph, parameters=None):
    """
    Applies the alignment algorithm to an event log without using multithreading.
    This function is used for smaller logs (less than 100 traces).

    Parameters:
    -----------
    log : EventLog
        The event log to be aligned.
    graph : DcrGraph
        The DCR graph against which the alignment will be performed.
    parameters : dict, optional
        Additional parameters for the alignment process.

    Returns:
    --------
    List[Dict[str, Any]]
        A list of dictionaries, each containing the alignment results for a single trace.
    """
    results = []
    for trace in log:
        alignment = TraceAlignment(graph, trace, parameters=parameters)
        trace_result = alignment.perform_alignment()
        results.extend(trace_result)
    return results

def apply_multithreaded(trace_or_log: Union[pd.DataFrame, EventLog, Trace], graph: DcrGraph, parameters=None):
    """
    Applies the multithreaded alignment algorithm to either a single trace or an entire event log.

    This function is similar to 'apply', but it always uses the multithreaded approach for log alignment,
    regardless of the log size. It's useful when processing large logs or when maximum performance is required.

    Parameters:
        trace_or_log (Union[pd.DataFrame, EventLog, Trace]): The input trace or log to be aligned.
        graph (DcrGraph): The DCR graph against which the alignment will be performed.
        parameters (dict, optional): Additional parameters for the alignment process.

    Returns:
        Union[List[Dict[str, Any]], Dict[str, Any]]: The alignment results. For a single trace,
        it returns a dictionary. For a log, it returns a list of dictionaries.
    """
    if isinstance(trace_or_log, Trace):
        alignment = TraceAlignment(graph, trace_or_log, parameters=parameters)
        return alignment.perform_alignment()
    else:
        if isinstance(trace_or_log, pd.DataFrame):
            trace_or_log = log_converter.apply(trace_or_log, variant=log_converter.Variants.TO_EVENT_LOG)

        parameters = parameters or {}
        parameters.setdefault("max_workers", 4)

        alignment = LogAlignment(trace_or_log, parameters=parameters)
        return alignment.perform_log_alignment(graph, parameters=parameters)



def get_diagnostics_dataframe(log: EventLog, conf_result: List[Dict[str, Any]], parameters=None) -> pd.DataFrame:
    """
    Gets the diagnostics dataframe from a log and the conformance results

    Parameters
    --------------
    log
        Event log
    conf_result
        Results of conformance checking
    variant
        Variant to be used:
        - Variants.CLASSIC
    parameters
        Variant-specific parameters

    Returns
    --------------
    diagn_dataframe
        Diagnostics dataframe
    """
    if parameters is None:
        parameters = {}

    case_id_key = exec_utils.get_param_value(Parameters.CASE_ID_KEY, parameters,
                                             xes_constants.DEFAULT_TRACEID_KEY)

    import pandas as pd
    diagn_stream = []
    for index in range(len(log)):
        case_id = log[index].attributes[case_id_key]
        align_fitness = conf_result[index][Outputs.ALIGN_FITNESS.value]
        diagn_stream.append({"case_id": case_id, "align_fitness": align_fitness})

    return pd.DataFrame(diagn_stream)

