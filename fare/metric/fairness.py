"""
Fairness measures from "Evaluating Fairness in Argument Retrieval"
Paper: https://doi.org/10.1145/3459637.3482099
Code: https://github.com/sachinpc1993/fair-arguments
"""
from abc import abstractmethod, ABC
from dataclasses import dataclass, field
from enum import Enum
from functools import cached_property
from math import log
from random import choice
from typing import Iterable, Union, Iterator, Hashable, Optional, Final, \
    Sequence

from ir_measures import DefaultPipeline
from ir_measures.measures import Measure, ParamInfo
from ir_measures.providers import Provider, Evaluator, register, registry
from ir_measures.util import (
    flatten_measures, QrelsConverter, RunConverter, Qrel, ScoredDoc, Metric
)
from pandas import DataFrame

Qrels = Union[Iterable[Qrel], dict[str, dict[str, int]], DataFrame]
Run = Union[Iterable[ScoredDoc], dict[str, dict[str, int]], DataFrame]


class ProtectedGroupStrategy(Enum):
    MINORITY = "minority"
    MAJORITY = "majority"


class TieBreakingStrategy(Enum):
    RANDOM = "random"
    GROUP_ASCENDING = "group-ascending"
    GROUP_DESCENDING = "group-descending"


class FairnessMeasure(Measure, ABC):
    SUPPORTED_PARAMS = {
        "cutoff": ParamInfo(
            dtype=int,
            required=False,
            desc="ranking cutoff threshold"
        ),
        "group_col": ParamInfo(
            dtype=Hashable,
            required=False,
            default="group",
            desc="group column in run"
        ),
        "groups": ParamInfo(
            dtype=set[Hashable],
            required=False,
            desc="group names"
        ),
        "protected_group": ParamInfo(
            dtype=Hashable,
            required=False,
            default=ProtectedGroupStrategy.MINORITY.value,
            desc="protected group name or selection strategy"
        ),
        "tie_breaking": ParamInfo(
            dtype=Hashable,
            required=False,
            desc="tie breaking strategy when selecting the protected group "
                 "using the minority or majority strategies or a "
                 "comma-separated preference list"
                 "(if not specified, ties will raise an exception)"
        ),
    }

    @cached_property
    def _cutoff_param(self) -> Optional[int]:
        if "cutoff" not in self.params:
            return None
        return self.params["cutoff"]

    @cached_property
    def _group_col_param(self) -> Hashable:
        if "group_col" not in self.params:
            return "group"
        return self.params["group_col"]

    @cached_property
    def _groups_param(self) -> Optional[set[Hashable]]:
        if "groups" not in self.params:
            return None
        return self.params["groups"]

    @cached_property
    def _protected_group_param(self) -> Union[
        ProtectedGroupStrategy, Hashable
    ]:
        if "protected_group" not in self.params:
            return ProtectedGroupStrategy.MINORITY
        protected_group = self.params["protected_group"]
        if protected_group in {s.value for s in ProtectedGroupStrategy}:
            return ProtectedGroupStrategy(protected_group)
        if not isinstance(protected_group, Hashable):
            raise ValueError(
                f"Illegal protected_group param: {protected_group}"
            )
        return protected_group

    @cached_property
    def _tie_breaking_param(self) -> Union[
        TieBreakingStrategy, Sequence, None
    ]:
        if "tie_breaking" not in self.params:
            return None
        tie_breaking = self.params["tie_breaking"]
        if tie_breaking is None:
            return None
        if tie_breaking in {s.value for s in TieBreakingStrategy}:
            return TieBreakingStrategy(tie_breaking)
        if not isinstance(tie_breaking, Hashable):
            raise ValueError(
                f"Illegal tie_breaking param: {tie_breaking}"
            )
        return [
            group.strip()
            for group in str(tie_breaking).split(",")
        ]

    @abstractmethod
    def fairness(
            self,
            ranking: DataFrame,
            group_col: Hashable,
            group_counts: dict[Hashable, int],
            protected_group: Hashable,
    ) -> float:
        pass

    def _group_counts(
            self,
            qrels: DataFrame,
    ) -> dict[Hashable, int]:
        counts = qrels.groupby(self._group_col_param).size().to_dict()
        counts = defaultdict(lambda: 0, counts)
        return counts

    def _protected_group(self, group_counts: dict[Hashable, int]) -> Hashable:
        protected_group: Hashable = self._protected_group_param
        if isinstance(protected_group, ProtectedGroupStrategy):
            strategy: ProtectedGroupStrategy = protected_group
            groups: list[tuple[Hashable, int]] = [
                item for item in group_counts.items()
            ]
            if strategy == ProtectedGroupStrategy.MINORITY:
                groups = sorted(groups, key=lambda item: item[1])
            elif strategy == ProtectedGroupStrategy.MAJORITY:
                groups = sorted(groups, key=lambda item: item[1], reverse=True)
            else:
                raise ValueError(
                    f"Unknown protected group strategy: {strategy}"
                )
            if len(groups) > 1 and groups[0][1] == groups[1][1]:
                # Tie in group selection.
                count = groups[0][1]
                tie_groups = [
                    group[0]
                    for group in groups
                    if group[1] == count
                ]
                tie_breaking = self._tie_breaking_param
                if tie_breaking is None:
                    raise ValueError(
                        f"Could not select protected group "
                        f"by {strategy.value} because of a tie. "
                        f"Groups {tie_groups} all occur {count} time(s)."
                    )
                elif isinstance(tie_breaking, TieBreakingStrategy):
                    if not all(hasattr(g, "__lt__") for g in tie_groups):
                        raise ValueError(
                            f"Tie breaking {tie_breaking.value} requires "
                            f"sorting but groups are not "
                            f"sortable: {tie_groups}"
                        )
                    if tie_breaking == TieBreakingStrategy.RANDOM:
                        return choice(tie_groups)
                    elif tie_breaking == TieBreakingStrategy.GROUP_ASCENDING:
                        # noinspection PyTypeChecker
                        return sorted(tie_groups)[0]
                    elif tie_breaking == TieBreakingStrategy.GROUP_DESCENDING:
                        # noinspection PyTypeChecker
                        return sorted(tie_groups, reverse=True)[0]
                else:
                    tie_breaking_groups = [
                        group
                        for group in tie_breaking
                        if group in tie_groups
                    ]
                    if len(tie_breaking_groups) == 0:
                        raise ValueError(
                            f"Tie breaking preference {tie_breaking} not "
                            f"applicable to resolve tie: {tie_groups}"
                        )
                    return tie_breaking_groups[0]
            return groups[0][0]
        else:
            return protected_group

    def _compute_query(
            self,
            qrels: DataFrame,
            ranking: DataFrame,
            groups: set[Hashable],
    ) -> float:
        group_counts = self._group_counts(qrels)
        protected_group = self._protected_group(group_counts)
        if protected_group not in groups:
            raise ValueError(
                f"Protected group {protected_group} "
                f"not found in groups {groups}."
            )
        return self.fairness(
            ranking,
            self._group_col_param,
            group_counts,
            protected_group,
        )

    def _groups(self, qrels: DataFrame) -> set[Hashable]:
        if self._groups_param is not None:
            return self._groups_param
        return set(qrels[self._group_col_param].unique().tolist())

    def compute(self, qrels: DataFrame, run: DataFrame) -> Iterator[Metric]:
        groups = self._groups(qrels)

        if self._cutoff_param is not None:
            # Assumes that results are already sorted.
            # (This is done in FairnessEvaluator.)
            run = run.groupby("query_id").head(self._cutoff_param).reset_index(
                drop=True)

        for qid, ranking in run.groupby("query_id"):
            yield Metric(
                str(qid),
                self,
                self._compute_query(
                    qrels[qrels["query_id"] == qid],
                    ranking,
                    groups,
                )
            )

    def __str__(self):
        name = self.NAME
        cutoff = ""
        if self._cutoff_param is not None:
            cutoff = f"@{self._cutoff_param}"
        group_col = None
        if self._group_col_param != "group":
            group_col = repr(self._group_col_param)
        protected_group = None
        if isinstance(self._protected_group_param, ProtectedGroupStrategy):
            if self._protected_group_param != ProtectedGroupStrategy.MINORITY:
                protected_group = repr(self._protected_group_param.value)
        elif isinstance(self._protected_group_param, Hashable):
            protected_group = repr(self._protected_group_param)
        tie_breaking = None
        if isinstance(self._tie_breaking_param, TieBreakingStrategy):
            tie_breaking = repr(self._tie_breaking_param.value)
        elif isinstance(self._tie_breaking_param, Sequence):
            tie_breaking = repr(",".join(self._tie_breaking_param))
        params = [
            f"{name}={param}"
            for name, param in {
                "group_col": group_col,
                "groups": self._groups_param,
                "protected_group": protected_group,
                "tie_breaking": tie_breaking,
            }.items()
            if param is not None
        ]
        return f"{name}{cutoff}({','.join(params)})"


class _NormalizedDiscountedDifference(FairnessMeasure):
    NAME = "rND"
    __name__ = "rND"

    def fairness(
            self,
            ranking: DataFrame,
            group_col: Hashable,
            group_counts: dict[Hashable, int],
            protected_group: Hashable,
    ) -> float:
        N = sum(group_counts.values())

        if 0 in ranking["rank"].values:
            # Some runs use zero-indexed ranks.
            ranking["rank"] += 1

        metrics = []
        # For each ranking position
        for i in ranking["rank"]:
            temp_ranking = ranking[ranking["rank"].isin(range(1, i + 1))]

            stance_freq = temp_ranking[group_col].value_counts()
            stance_freq_df = stance_freq.to_frame().reset_index().rename(
                columns={
                    group_col: "count",
                    "index": "group"
                })

            if len(stance_freq_df[
                       stance_freq_df["group"] == protected_group
                   ]["count"]) == 0:
                S_Plus_in_i = 0
            else:
                S_Plus_in_i = list(stance_freq_df[
                                       stance_freq_df[
                                           "group"] == protected_group
                                       ]["count"])[0]

            S_plus = group_counts[protected_group]

            intermediate_metric = (
                    (1 / log(i + 1, 2)) *
                    abs(abs(S_Plus_in_i / (i + 1)) - abs(S_plus / N))
            )

            metrics.append(intermediate_metric)

        return sum(metrics)


NormalizedDiscountedDifference = _NormalizedDiscountedDifference()
rND = NormalizedDiscountedDifference


@dataclass
class FairnessEvaluator(Evaluator):
    measures: Final[Iterable[FairnessMeasure]] = field()
    qrels: DataFrame

    def __post_init__(self):
        super().__init__(self.measures, set(self.qrels["query_id"].unique()))

    def _iter_calc(self, run: Run) -> Iterator[Metric]:
        run: DataFrame = RunConverter(run).as_pd_dataframe()
        run.sort_values(
            by=["query_id", "score"],
            ascending=[True, False],
            inplace=True,
        )
        for measure in self.measures:
            yield from measure.compute(self.qrels, run)


@dataclass(frozen=True)
class FairnessProvider(Provider):
    NAME = "fairness"
    SUPPORTED_MEASURES = [
        rND
    ]
    _is_available = True

    def _evaluator(
            self,
            measures: Iterable[FairnessMeasure],
            qrels: Qrels
    ) -> FairnessEvaluator:
        measures = flatten_measures(measures)
        qrels: DataFrame = QrelsConverter(qrels).as_pd_dataframe()
        qrels.sort_values(
            by=["query_id", "doc_id"],
            inplace=True,
        )
        return FairnessEvaluator(measures, qrels)


register(FairnessProvider())
DefaultPipeline.providers.append(registry["fairness"])
