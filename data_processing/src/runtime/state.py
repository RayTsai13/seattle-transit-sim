"""Scenario state versioning for interactive heatmap sessions."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.runtime.clock import SimTime
from src.runtime.stores import ScenarioDeltaStore


BASELINE_STATE_VERSION = "state_baseline"


@dataclass
class ScenarioRecord:
    scenario_id: str
    scenario_type: str
    state_before: str
    state_after: str
    created_at_real_time: float
    created_at_sim_time: SimTime
    effective_from_tick: int
    effective_from_sim_time: SimTime
    payload: dict[str, Any]
    delta_store: ScenarioDeltaStore
    status: str = "ready"

    def is_effective(self, tick: int) -> bool:
        return self.status == "ready" and tick >= self.effective_from_tick

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "scenario_type": self.scenario_type,
            "state_before": self.state_before,
            "state_after": self.state_after,
            "created_at_real_time": self.created_at_real_time,
            "created_at_sim_time": self.created_at_sim_time.to_dict(),
            "effective_from_tick": self.effective_from_tick,
            "effective_from_sim_time": self.effective_from_sim_time.to_dict(),
            "status": self.status,
            "delta_source": str(self.delta_store.loaded_from),
            "delta_frame_count": self.delta_store.frame_count,
            "delta_changed_cells": self.delta_store.changed_cells,
        }


class ScenarioStateManager:
    """Tracks immutable scenario states and their rebased deltas."""

    def __init__(self) -> None:
        self.current_state_version = BASELINE_STATE_VERSION
        self._state_counter = 0
        self._records: list[ScenarioRecord] = []
        self._records_by_id: dict[str, ScenarioRecord] = {}
        self._records_by_state: dict[str, ScenarioRecord] = {}

    @property
    def records(self) -> list[ScenarioRecord]:
        return list(self._records)

    def reset(self) -> None:
        self.current_state_version = BASELINE_STATE_VERSION
        self._state_counter = 0
        self._records.clear()
        self._records_by_id.clear()
        self._records_by_state.clear()

    def register_precomputed_delta(
        self,
        *,
        payload: dict[str, Any],
        delta_csv: Path,
        current_tick: int,
        current_sim_time: SimTime,
        effective_from_tick: int | None,
        effective_from_sim_time: SimTime,
    ) -> ScenarioRecord:
        """Register a scenario delta already produced by the demand pipeline.

        The delta CSV should represent `score(state_after) - score(state_before)`.
        That keeps network interactions correct when scenarios are applied after
        earlier user edits.
        """
        scenario_id = str(payload.get("scenario_id") or uuid.uuid4())
        if scenario_id in self._records_by_id:
            raise ValueError(f"scenario_id already exists: {scenario_id}")

        self._state_counter += 1
        state_before = self.current_state_version
        state_after = str(payload.get("state_after") or f"state_v{self._state_counter}")
        delta_store = ScenarioDeltaStore.from_csv(delta_csv)
        record = ScenarioRecord(
            scenario_id=scenario_id,
            scenario_type=str(payload.get("type", "precomputed_delta")),
            state_before=state_before,
            state_after=state_after,
            created_at_real_time=time.time(),
            created_at_sim_time=current_sim_time,
            effective_from_tick=current_tick if effective_from_tick is None else effective_from_tick,
            effective_from_sim_time=effective_from_sim_time,
            payload=payload,
            delta_store=delta_store,
        )

        self.current_state_version = state_after
        self._records.append(record)
        self._records_by_id[scenario_id] = record
        self._records_by_state[state_after] = record
        return record

    def active_records(self, tick: int) -> list[ScenarioRecord]:
        return [record for record in self._records if record.is_effective(tick)]

    def get_scenario(self, scenario_id: str) -> ScenarioRecord | None:
        return self._records_by_id.get(scenario_id)

    def get_state_delta(self, state_version: str) -> ScenarioRecord | None:
        return self._records_by_state.get(state_version)

    def to_dict(self, *, current_tick: int, current_sim_time: SimTime) -> dict[str, Any]:
        return {
            "state_version": self.current_state_version,
            "current_tick": current_tick,
            "sim_time": current_sim_time.to_dict(),
            "scenarios": [record.to_dict() for record in self._records],
        }
