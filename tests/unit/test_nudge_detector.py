"""Unit tests for the NudgeDetector stuck-state detection system."""

from henchmen.operative.nudge_detector import NudgeDetector, StuckState


class TestNudgeDetectorStuckStates:
    def test_not_stuck_initially(self):
        nd = NudgeDetector(max_steps=30)
        assert nd.check_stuck(0) is None

    def test_repeated_tool_error(self):
        nd = NudgeDetector(max_steps=30)
        nd.record_tool_call("file_edit", success=False)
        nd.record_tool_call("file_edit", success=False)
        nd.record_tool_call("file_edit", success=False)
        assert nd.check_stuck(5) == StuckState.REPEATED_TOOL_ERROR

    def test_different_tools_reset_error_count(self):
        nd = NudgeDetector(max_steps=30)
        nd.record_tool_call("file_edit", success=False)
        nd.record_tool_call("file_edit", success=False)
        nd.record_tool_call("grep_search", success=False)  # different tool
        assert nd.check_stuck(5) is None

    def test_search_loop_detected(self):
        nd = NudgeDetector(max_steps=30)
        for _ in range(6):
            nd.record_tool_call("grep_search")
        assert nd.check_stuck(8) == StuckState.SEARCH_LOOP

    def test_mixed_search_tools_also_detected(self):
        nd = NudgeDetector(max_steps=30)
        nd.record_tool_call("grep_search")
        nd.record_tool_call("file_read")
        nd.record_tool_call("code_search")
        nd.record_tool_call("find_file")
        nd.record_tool_call("grep_search")
        nd.record_tool_call("file_read")
        assert nd.check_stuck(8) == StuckState.SEARCH_LOOP

    def test_high_budget_no_commit(self):
        nd = NudgeDetector(max_steps=10)
        nd.record_tool_call("file_edit")  # marks has_edited
        # At step 7 (70%), should trigger
        assert nd.check_stuck(7) == StuckState.HIGH_BUDGET_NO_COMMIT

    def test_high_budget_with_commit_ok(self):
        nd = NudgeDetector(max_steps=10)
        nd.record_tool_call("file_edit")
        nd.record_tool_call("git_commit")
        assert nd.check_stuck(7) is None

    def test_text_only_loop_with_edits(self):
        nd = NudgeDetector(max_steps=30)
        nd.record_tool_call("file_edit")
        nd.record_text_only_response()
        nd.record_text_only_response()
        nd.record_text_only_response()
        assert nd.check_stuck(10) == StuckState.TEXT_ONLY_LOOP

    def test_text_only_loop_without_edits(self):
        nd = NudgeDetector(max_steps=30)
        nd.record_text_only_response()
        nd.record_text_only_response()
        assert nd.check_stuck(6) == StuckState.TEXT_ONLY_LOOP

    def test_read_only_loop(self):
        nd = NudgeDetector(max_steps=24)
        # Threshold is max(3, 24//8) = 3
        nd.record_tool_call("file_read")
        nd.record_tool_call("file_read")
        nd.record_tool_call("file_read")
        assert nd.check_stuck(3) == StuckState.READ_ONLY_LOOP

    def test_repeated_edit_detected(self):
        nd = NudgeDetector(max_steps=30)
        for _ in range(5):
            nd.record_tool_call("file_edit")
        assert nd.check_stuck(10) == StuckState.REPEATED_EDIT


class TestNudgeMessages:
    def test_all_stuck_states_have_messages(self):
        nd = NudgeDetector(max_steps=30)
        for state in StuckState:
            msg = nd.get_nudge_message(state, 15)
            assert msg, f"No message for {state}"
            assert "steps" in msg.lower() or "tool" in msg.lower() or "commit" in msg.lower()

    def test_message_includes_remaining_steps(self):
        nd = NudgeDetector(max_steps=30)
        msg = nd.get_nudge_message(StuckState.HIGH_BUDGET_NO_COMMIT, 20)
        assert "10" in msg  # 30 - 20 = 10 remaining

    def test_text_only_with_edits_mentions_commit(self):
        nd = NudgeDetector(max_steps=30)
        nd.record_tool_call("file_edit")
        nd.record_text_only_response()
        nd.record_text_only_response()
        nd.record_text_only_response()
        msg = nd.get_nudge_message(StuckState.TEXT_ONLY_LOOP, 10)
        assert "commit" in msg.lower()


class TestStepBudgetDefaults:
    def test_scheme_node_get_effective_budget(self):
        from henchmen.models.scheme import NodeType, SchemeNode

        node = SchemeNode(id="implement_fix", name="Fix Bug", node_type=NodeType.AGENTIC, max_steps=30)
        budget = node.get_effective_budget()
        assert budget.base_steps == 30
        assert budget.max_steps == 50

    def test_scheme_node_with_explicit_budget(self):
        from henchmen.models.scheme import NodeType, SchemeNode, StepBudget

        custom = StepBudget(base_steps=15, max_steps=25, extension_steps=5, max_extensions=1)
        node = SchemeNode(
            id="implement_fix", name="Fix Bug", node_type=NodeType.AGENTIC, max_steps=30, step_budget=custom
        )
        budget = node.get_effective_budget()
        assert budget.base_steps == 15
        assert budget.max_steps == 25

    def test_unknown_node_id_constructs_from_max_steps(self):
        from henchmen.models.scheme import NodeType, SchemeNode

        node = SchemeNode(id="custom_node", name="Custom", node_type=NodeType.AGENTIC, max_steps=40)
        budget = node.get_effective_budget()
        assert budget.base_steps == 40
        assert budget.max_steps == 40
        assert budget.extension_steps == 0
