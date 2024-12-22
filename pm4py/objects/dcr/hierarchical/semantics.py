from typing import Set
from pm4py.objects.dcr.obj import DcrGraph
from pm4py.objects.dcr.hierarchical.obj import HierarchicalDcrGraph
from pm4py.objects.dcr.semantics import DcrSemantics


class HiearchicalSemantics(DcrSemantics):

    @classmethod
    def execute(cls, graph: HierarchicalDcrGraph, event):
        if event in graph.nestedgroups:
            raise ValueError ("cannot run nested groups")
        if not cls.is_enabled(event, graph):
            raise ValueError(f"Event {event} cannot be executed because its conditions are not met.")


        def Operation_on_group_or_event(e_prime, operation):
            if e_prime in graph.nestedgroups:
                # If e_prime is a group, function to on all events in the group
                for event in graph.nestedgroups[e_prime]:
                    Operation_on_group_or_event(event, operation)
            else:
                # If e_prime is an individual event, apply the operation directly
                operation(e_prime)
        def execute_event(e_prime):
            if e_prime in graph.excludes:
                for event in graph.excludes[e_prime]:
                    Operation_on_group_or_event(event, lambda event: graph.marking.included.discard(event))
            if e_prime in graph.includes:
                for event in graph.includes[e_prime]:
                    Operation_on_group_or_event(event, lambda event:  graph.marking.included.add(event))
            if e_prime in graph.responses:
                for event  in graph.responses[e_prime]:
                    Operation_on_group_or_event(event, lambda event:  graph.marking.pending.add(event))

            if e_prime in graph.nestedgroups_map:
                group = graph.nestedgroups_map[e_prime]
                execute_event(group)
            
        if event in graph.marking.pending:
            graph.marking.pending.discard(event)
        
        
        execute_event(event)
        graph.marking.executed.add(event)
        
        return graph
    @classmethod
    def enabled(cls, graph: HierarchicalDcrGraph) -> Set[str]:

        def execute_event(e_prime, operation):
            if e_prime in graph.nestedgroups:
                # If e_prime is a group, function to on all events in the group
                for event in graph.nestedgroups[e_prime]:
                    execute_event(event, operation)
            else:
                # If e_prime is an individual event, apply the operation directly
                operation(e_prime)
        def flat(e_prime_list:set):
            f = set()
            for e_prime in e_prime_list:
                if e_prime in graph.nestedgroups:
                    for event in graph.nestedgroups[e_prime]:
                        f = f | flat({event})
                else:
                    f = f | {e_prime}
            return f
        # can be extended to check for milestones
        res = set(graph.marking.included)
        for e in set(graph.conditions.keys()).intersection(res) or set(graph.conditions.keys()).intersection(graph.nestedgroups.keys()):
            if len(flat(graph.conditions[e]).intersection(graph.marking.included.difference(
                    graph.marking.executed))) > 0:
                execute_event(e, lambda event: res.discard(event))
        return res




