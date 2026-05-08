"""Tests for group management in hub.py."""

import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hub


def _make_registry(groups=None):
    registry = {
        "version": 2,
        "agents": {
            "agent-a": {"type": "claudecode", "provider": "cc_connect", "work_dir": "/tmp",
                         "capabilities": ["message.send"], "labels": []},
            "agent-b": {"type": "codex", "provider": "cc_connect", "work_dir": "/tmp",
                         "capabilities": ["message.send"], "labels": []},
            "agent-c": {"type": "claudecode", "provider": "cdp", "work_dir": "/tmp",
                         "capabilities": ["message.send"], "labels": []},
        },
    }
    if groups is not None:
        registry["groups"] = groups
    return registry


class GetGroupsTest(unittest.TestCase):
    def test_returns_empty_when_no_groups(self):
        registry = _make_registry()
        self.assertEqual(hub.get_groups(registry), {})

    def test_returns_groups_dict(self):
        groups = {"default": {"members": ["agent-a", "agent-b"]}}
        registry = _make_registry(groups)
        self.assertEqual(hub.get_groups(registry), groups)


class GetAgentGroupsTest(unittest.TestCase):
    def test_agent_in_one_group(self):
        groups = {"g1": {"members": ["agent-a"]}}
        registry = _make_registry(groups)
        self.assertEqual(hub.get_agent_groups(registry, "agent-a"), ["g1"])

    def test_agent_in_multiple_groups(self):
        groups = {
            "g1": {"members": ["agent-a"]},
            "g2": {"members": ["agent-a", "agent-b"]},
        }
        registry = _make_registry(groups)
        result = hub.get_agent_groups(registry, "agent-a")
        self.assertEqual(sorted(result), ["g1", "g2"])

    def test_agent_in_no_group(self):
        groups = {"g1": {"members": ["agent-b"]}}
        registry = _make_registry(groups)
        self.assertEqual(hub.get_agent_groups(registry, "agent-a"), [])

    def test_no_groups_defined(self):
        registry = _make_registry()
        self.assertEqual(hub.get_agent_groups(registry, "agent-a"), [])


class GetGroupMembersTest(unittest.TestCase):
    def test_returns_members(self):
        groups = {"g1": {"members": ["agent-a", "agent-b"]}}
        registry = _make_registry(groups)
        self.assertEqual(hub.get_group_members(registry, "g1"), ["agent-a", "agent-b"])

    def test_unknown_group_raises(self):
        registry = _make_registry({})
        with self.assertRaises(KeyError):
            hub.get_group_members(registry, "nonexistent")


class CheckGroupCompatibilityTest(unittest.TestCase):
    def test_same_group_is_compatible(self):
        groups = {"g1": {"members": ["agent-a", "agent-b"]}}
        registry = _make_registry(groups)
        self.assertTrue(hub.check_group_compatibility(registry, "agent-a", "agent-b"))

    def test_different_groups_incompatible(self):
        groups = {
            "g1": {"members": ["agent-a"]},
            "g2": {"members": ["agent-b"]},
        }
        registry = _make_registry(groups)
        self.assertFalse(hub.check_group_compatibility(registry, "agent-a", "agent-b"))

    def test_no_groups_is_compatible(self):
        registry = _make_registry()
        self.assertTrue(hub.check_group_compatibility(registry, "agent-a", "agent-b"))

    def test_agent_not_in_any_group_is_compatible(self):
        groups = {"g1": {"members": ["agent-a"]}}
        registry = _make_registry(groups)
        self.assertTrue(hub.check_group_compatibility(registry, "agent-a", "agent-c"))


class CmdGroupsListTest(unittest.TestCase):
    def test_list_empty(self):
        registry = _make_registry({})
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(registry, f)
            f.flush()
            old_path = hub.REGISTRY_PATH
            hub.REGISTRY_PATH = Path(f.name)
            try:
                args = hub.parse_args(["groups"])
                result = hub.cmd_groups(args)
                self.assertEqual(result, 0)
            finally:
                hub.REGISTRY_PATH = old_path
                Path(f.name).unlink(missing_ok=True)

    def test_list_with_groups(self):
        groups = {"default": {"description": "Main", "members": ["agent-a"]}}
        registry = _make_registry(groups)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(registry, f)
            f.flush()
            old_path = hub.REGISTRY_PATH
            hub.REGISTRY_PATH = Path(f.name)
            try:
                args = hub.parse_args(["groups"])
                result = hub.cmd_groups(args)
                self.assertEqual(result, 0)
            finally:
                hub.REGISTRY_PATH = old_path
                Path(f.name).unlink(missing_ok=True)


class CmdGroupsCreateDeleteTest(unittest.TestCase):
    def test_create_and_delete(self):
        registry = _make_registry({})
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(registry, f)
            f.flush()
            old_path = hub.REGISTRY_PATH
            hub.REGISTRY_PATH = Path(f.name)
            try:
                # Create
                args = hub.parse_args(["groups", "create", "new-group", "--description", "test"])
                result = hub.cmd_groups(args)
                self.assertEqual(result, 0)

                # Verify it exists
                data = json.loads(Path(f.name).read_text())
                self.assertIn("new-group", data["groups"])
                self.assertEqual(data["groups"]["new-group"]["description"], "test")

                # Create duplicate should fail
                result = hub.cmd_groups(args)
                self.assertEqual(result, 1)

                # Delete
                args = hub.parse_args(["groups", "delete", "new-group"])
                result = hub.cmd_groups(args)
                self.assertEqual(result, 0)

                # Verify it's gone
                data = json.loads(Path(f.name).read_text())
                self.assertNotIn("new-group", data["groups"])
            finally:
                hub.REGISTRY_PATH = old_path
                Path(f.name).unlink(missing_ok=True)


class CmdGroupsJoinLeaveTest(unittest.TestCase):
    def test_join_and_leave(self):
        groups = {"g1": {"members": ["agent-a"]}}
        registry = _make_registry(groups)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(registry, f)
            f.flush()
            old_path = hub.REGISTRY_PATH
            hub.REGISTRY_PATH = Path(f.name)
            try:
                # Join
                args = hub.parse_args(["groups", "join", "g1", "agent-b"])
                result = hub.cmd_groups(args)
                self.assertEqual(result, 0)

                data = json.loads(Path(f.name).read_text())
                self.assertIn("agent-b", data["groups"]["g1"]["members"])

                # Join again (idempotent)
                result = hub.cmd_groups(args)
                self.assertEqual(result, 0)

                # Leave
                args = hub.parse_args(["groups", "leave", "g1", "agent-b"])
                result = hub.cmd_groups(args)
                self.assertEqual(result, 0)

                data = json.loads(Path(f.name).read_text())
                self.assertNotIn("agent-b", data["groups"]["g1"]["members"])
            finally:
                hub.REGISTRY_PATH = old_path
                Path(f.name).unlink(missing_ok=True)

    def test_join_unknown_group_fails(self):
        registry = _make_registry({})
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(registry, f)
            f.flush()
            old_path = hub.REGISTRY_PATH
            hub.REGISTRY_PATH = Path(f.name)
            try:
                args = hub.parse_args(["groups", "join", "nonexistent", "agent-a"])
                result = hub.cmd_groups(args)
                self.assertEqual(result, 1)
            finally:
                hub.REGISTRY_PATH = old_path
                Path(f.name).unlink(missing_ok=True)


class ParseArgsTest(unittest.TestCase):
    def test_list_group_flag(self):
        args = hub.parse_args(["list", "--group", "default"])
        self.assertEqual(args.group, "default")

    def test_send_group_flag(self):
        args = hub.parse_args(["send", "agent-a", "hello", "--group", "g1"])
        self.assertEqual(args.group, "g1")

    def test_groups_create(self):
        args = hub.parse_args(["groups", "create", "mygroup", "--description", "test"])
        self.assertEqual(args.groups_command, "create")
        self.assertEqual(args.name, "mygroup")
        self.assertEqual(args.description, "test")

    def test_groups_join(self):
        args = hub.parse_args(["groups", "join", "g1", "agent-a"])
        self.assertEqual(args.groups_command, "join")
        self.assertEqual(args.group, "g1")
        self.assertEqual(args.agent, "agent-a")

    def test_relay(self):
        args = hub.parse_args(["relay", "agent-a", "agent-b", "hello"])
        self.assertEqual(args.command, "relay")
        self.assertEqual(args.from_agent, "agent-a")
        self.assertEqual(args.to_agent, "agent-b")
        self.assertEqual(args.message, "hello")

    def test_relay_no_wait_flag(self):
        """relay should not have --wait (always waits)."""
        args = hub.parse_args(["relay", "a", "b", "msg"])
        self.assertFalse(hasattr(args, "wait") and args.wait is True)


class ListGroupJsonTest(unittest.TestCase):
    def test_json_output_includes_groups(self):
        groups = {"g1": {"members": ["agent-a"]}}
        registry = _make_registry(groups)
        bindings = {
            "cc_connect": {"agent-a": {"webhook_port": 0, "session_key": ""}},
            "cdp": {},
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as rf:
            json.dump(registry, rf)
            rf.flush()
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as bf:
                json.dump(bindings, bf)
                bf.flush()
                old_reg = hub.REGISTRY_PATH
                old_bind = hub.BINDINGS_PATH
                hub.REGISTRY_PATH = Path(rf.name)
                hub.BINDINGS_PATH = Path(bf.name)
                try:
                    args = hub.parse_args(["list", "--format", "json", "--group", "g1"])
                    import io
                    captured = io.StringIO()
                    old_stdout = sys.stdout
                    sys.stdout = captured
                    try:
                        hub.cmd_list(args)
                    finally:
                        sys.stdout = old_stdout
                    output = json.loads(captured.getvalue())
                    self.assertEqual(len(output), 1)
                    self.assertEqual(output[0]["name"], "agent-a")
                    self.assertIn("groups", output[0])
                finally:
                    hub.REGISTRY_PATH = old_reg
                    hub.BINDINGS_PATH = old_bind
                    Path(rf.name).unlink(missing_ok=True)
                    Path(bf.name).unlink(missing_ok=True)


class CmdSendGroupTest(unittest.TestCase):
    def test_send_unknown_group_fails(self):
        registry = _make_registry({})
        bindings = {"cc_connect": {}, "cdp": {}}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(registry, f)
            f.flush()
            old_path = hub.REGISTRY_PATH
            hub.REGISTRY_PATH = Path(f.name)
            try:
                args = hub.parse_args(["send", "agent-a", "hello", "--group", "nonexistent"])
                result = hub.cmd_send(args)
                self.assertEqual(result, 1)
            finally:
                hub.REGISTRY_PATH = old_path
                Path(f.name).unlink(missing_ok=True)

    def test_send_non_member_fails(self):
        groups = {"g1": {"members": ["agent-a"]}}
        registry = _make_registry(groups)
        bindings = {"cc_connect": {}, "cdp": {}}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(registry, f)
            f.flush()
            old_path = hub.REGISTRY_PATH
            hub.REGISTRY_PATH = Path(f.name)
            try:
                args = hub.parse_args(["send", "agent-b", "hello", "--group", "g1"])
                result = hub.cmd_send(args)
                self.assertEqual(result, 1)
            finally:
                hub.REGISTRY_PATH = old_path
                Path(f.name).unlink(missing_ok=True)


class MutateRegistryGroupsTest(unittest.TestCase):
    def test_atomic_create_and_delete(self):
        registry = _make_registry({})
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(registry, f)
            f.flush()
            old_path = hub.REGISTRY_PATH
            hub.REGISTRY_PATH = Path(f.name)
            try:
                hub.mutate_registry_groups(lambda g: g.__setitem__("test", {"members": []}))
                data = json.loads(Path(f.name).read_text())
                self.assertIn("test", data["groups"])

                hub.mutate_registry_groups(lambda g: g.__delitem__("test"))
                data = json.loads(Path(f.name).read_text())
                self.assertNotIn("test", data["groups"])
            finally:
                hub.REGISTRY_PATH = old_path
                Path(f.name).unlink(missing_ok=True)


class CmdRelayCrossGroupTest(unittest.TestCase):
    def test_relay_cross_group_rejected(self):
        groups = {
            "g1": {"members": ["agent-a"]},
            "g2": {"members": ["agent-b"]},
        }
        registry = _make_registry(groups)
        bindings = {
            "cc_connect": {
                "agent-a": {"webhook_port": 0, "session_key": "feishu:x:y"},
                "agent-b": {"webhook_port": 0, "session_key": "feishu:x:z"},
            },
            "cdp": {},
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as rf:
            json.dump(registry, rf)
            rf.flush()
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as bf:
                json.dump(bindings, bf)
                bf.flush()
                old_reg = hub.REGISTRY_PATH
                old_bind = hub.BINDINGS_PATH
                hub.REGISTRY_PATH = Path(rf.name)
                hub.BINDINGS_PATH = Path(bf.name)
                try:
                    args = hub.parse_args(["relay", "agent-a", "agent-b", "hello"])
                    result = hub.cmd_relay(args)
                    self.assertEqual(result, 1)
                finally:
                    hub.REGISTRY_PATH = old_reg
                    hub.BINDINGS_PATH = old_bind
                    Path(rf.name).unlink(missing_ok=True)
                    Path(bf.name).unlink(missing_ok=True)

    def test_relay_no_group_allowed(self):
        """Agents with no group can relay to each other."""
        registry = _make_registry({})
        bindings = {
            "cc_connect": {
                "agent-a": {"webhook_port": 0, "session_key": "feishu:x:y"},
                "agent-b": {"webhook_port": 0, "session_key": "feishu:x:z"},
            },
            "cdp": {},
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as rf:
            json.dump(registry, rf)
            rf.flush()
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as bf:
                json.dump(bindings, bf)
                bf.flush()
                old_reg = hub.REGISTRY_PATH
                old_bind = hub.BINDINGS_PATH
                hub.REGISTRY_PATH = Path(rf.name)
                hub.BINDINGS_PATH = Path(bf.name)
                try:
                    args = hub.parse_args(["relay", "agent-a", "agent-b", "hello"])
                    # This will fail at deliver() since no real webhook is running,
                    # but it should NOT fail at the group check.
                    with unittest.mock.patch("providers.cc_connect.CCConnectProvider.deliver") as mock_deliver:
                        from core.envelope import DeliveryReceipt
                        mock_deliver.return_value = DeliveryReceipt(
                            request_id="x", status="failed", provider="cc_connect",
                            delivered_at=0, error="no webhook",
                        )
                        result = hub.cmd_relay(args)
                    # Should fail at delivery, not at group check
                    self.assertEqual(result, 1)
                finally:
                    hub.REGISTRY_PATH = old_reg
                    hub.BINDINGS_PATH = old_bind
                    Path(rf.name).unlink(missing_ok=True)
                    Path(bf.name).unlink(missing_ok=True)


class BootstrapContextTest(unittest.TestCase):
    def test_print_does_not_write(self):
        registry = _make_registry({})
        bindings = {"cc_connect": {}, "cdp": {}}
        with tempfile.TemporaryDirectory() as tmpdir:
            old_reg = hub.REGISTRY_PATH
            old_bind = hub.BINDINGS_PATH
            old_cwd = Path.cwd()
            reg_path = Path(tmpdir) / "registry.json"
            bind_path = Path(tmpdir) / "bindings.json"
            reg_path.write_text(json.dumps(registry))
            bind_path.write_text(json.dumps(bindings))
            hub.REGISTRY_PATH = reg_path
            hub.BINDINGS_PATH = bind_path
            try:
                import os
                os.chdir(tmpdir)
                args = hub.parse_args(["bootstrap", "context", "--print"])
                result = hub.cmd_bootstrap_context(args)
                self.assertEqual(result, 0)
                # No files should be written
                self.assertFalse((Path(tmpdir) / "AGENTS.md").exists())
            finally:
                os.chdir(str(old_cwd))
                hub.REGISTRY_PATH = old_reg
                hub.BINDINGS_PATH = old_bind

    def test_write_then_check_ok(self):
        registry = _make_registry({})
        bindings = {"cc_connect": {}, "cdp": {}}
        with tempfile.TemporaryDirectory() as tmpdir:
            old_reg = hub.REGISTRY_PATH
            old_bind = hub.BINDINGS_PATH
            old_cwd = Path.cwd()
            reg_path = Path(tmpdir) / "registry.json"
            bind_path = Path(tmpdir) / "bindings.json"
            reg_path.write_text(json.dumps(registry))
            bind_path.write_text(json.dumps(bindings))
            hub.REGISTRY_PATH = reg_path
            hub.BINDINGS_PATH = bind_path
            try:
                import os
                os.chdir(tmpdir)
                # Write
                args = hub.parse_args(["bootstrap", "context", "--write"])
                result = hub.cmd_bootstrap_context(args)
                self.assertEqual(result, 0)
                self.assertTrue((Path(tmpdir) / "AGENTS.md").exists())
                # Check should pass
                args = hub.parse_args(["bootstrap", "context", "--check"])
                result = hub.cmd_bootstrap_context(args)
                self.assertEqual(result, 0)
            finally:
                os.chdir(str(old_cwd))
                hub.REGISTRY_PATH = old_reg
                hub.BINDINGS_PATH = old_bind

    def test_write_creates_backup(self):
        registry = _make_registry({})
        bindings = {"cc_connect": {}, "cdp": {}}
        with tempfile.TemporaryDirectory() as tmpdir:
            old_reg = hub.REGISTRY_PATH
            old_bind = hub.BINDINGS_PATH
            old_cwd = Path.cwd()
            reg_path = Path(tmpdir) / "registry.json"
            bind_path = Path(tmpdir) / "bindings.json"
            reg_path.write_text(json.dumps(registry))
            bind_path.write_text(json.dumps(bindings))
            hub.REGISTRY_PATH = reg_path
            hub.BINDINGS_PATH = bind_path
            try:
                import os
                os.chdir(tmpdir)
                # Create existing file
                agents_path = Path(tmpdir) / "AGENTS.md"
                agents_path.write_text("old content")
                # Write should backup
                args = hub.parse_args(["bootstrap", "context", "--write"])
                hub.cmd_bootstrap_context(args)
                self.assertTrue((Path(tmpdir) / "AGENTS.md.bak").exists())
                self.assertEqual((Path(tmpdir) / "AGENTS.md.bak").read_text(), "old content")
            finally:
                os.chdir(str(old_cwd))
                hub.REGISTRY_PATH = old_reg
                hub.BINDINGS_PATH = old_bind

    def test_no_duplication_in_output(self):
        """Generated AGENTS.md should not have duplicate sections."""
        registry = _make_registry({})
        bindings = {"cc_connect": {}, "cdp": {}}
        with tempfile.TemporaryDirectory() as tmpdir:
            old_reg = hub.REGISTRY_PATH
            old_bind = hub.BINDINGS_PATH
            old_cwd = Path.cwd()
            reg_path = Path(tmpdir) / "registry.json"
            bind_path = Path(tmpdir) / "bindings.json"
            reg_path.write_text(json.dumps(registry))
            bind_path.write_text(json.dumps(bindings))
            hub.REGISTRY_PATH = reg_path
            hub.BINDINGS_PATH = bind_path
            try:
                import os
                os.chdir(tmpdir)
                # Write twice
                args = hub.parse_args(["bootstrap", "context", "--write"])
                hub.cmd_bootstrap_context(args)
                hub.cmd_bootstrap_context(args)
                content = (Path(tmpdir) / "AGENTS.md").read_text()
                # Should not have duplicate "## Available Peers"
                self.assertEqual(content.count("## Available Peers"), 1)
            finally:
                os.chdir(str(old_cwd))
                hub.REGISTRY_PATH = old_reg
                hub.BINDINGS_PATH = old_bind


def _make_bindings(registry):
    """Create a bindings dict matching the agents in registry."""
    bindings = {"cc_connect": {}, "cdp": {}}
    for name, info in registry.get("agents", {}).items():
        provider = info["provider"]
        bindings.setdefault(provider, {})[name] = {
            "session_key": "sess:%s" % name,
            "webhook_host": "127.0.0.1",
            "webhook_port": 9110,
            "webhook_path": "/hook",
        }
    return bindings


class ResolveAgentTest(unittest.TestCase):
    """Tests for resolve_agent — exact match, fuzzy match, same-group preference."""

    def test_exact_match_returns_agent(self):
        registry = _make_registry(groups={})
        bindings = _make_bindings(registry)
        agent = hub.resolve_agent(registry, bindings, "agent-a")
        self.assertEqual(agent["name"], "agent-a")

    def test_exact_match_always_preferred(self):
        """Even if another agent's name contains the query, exact wins."""
        registry = _make_registry(groups={})
        registry["agents"]["codex-main"] = {"type": "codex", "provider": "cc_connect",
                                             "work_dir": "/tmp", "capabilities": [], "labels": []}
        bindings = _make_bindings(registry)
        agent = hub.resolve_agent(registry, bindings, "agent-b")
        self.assertEqual(agent["name"], "agent-b")

    def test_fuzzy_by_type(self):
        registry = _make_registry(groups={})
        bindings = _make_bindings(registry)
        # "codex" should match agent-b (type=codex)
        agent = hub.resolve_agent(registry, bindings, "codex")
        self.assertEqual(agent["name"], "agent-b")

    def test_fuzzy_by_name_substring(self):
        registry = _make_registry(groups={})
        bindings = _make_bindings(registry)
        # "agent" matches all three, no sender → should raise
        with self.assertRaises(KeyError) as ctx:
            hub.resolve_agent(registry, bindings, "agent")
        self.assertIn("Multiple agents", str(ctx.exception))

    def test_fuzzy_same_group_preference(self):
        """When multiple candidates match, prefer the one in sender's group."""
        groups = {
            "alpha": {"description": "", "members": ["agent-a", "agent-c"]},
            "beta": {"description": "", "members": ["agent-b"]},
        }
        registry = _make_registry(groups=groups)
        bindings = _make_bindings(registry)
        # "agent" matches a, b, c. Sender=agent-a (group alpha) → should pick agent-c
        agent = hub.resolve_agent(registry, bindings, "agent", sender="agent-a")
        self.assertEqual(agent["name"], "agent-c")

    def test_fuzzy_same_group_single_match(self):
        """Fuzzy match + same-group narrows to exactly one."""
        groups = {
            "alpha": {"description": "", "members": ["agent-a"]},
            "beta": {"description": "", "members": ["agent-b"]},
        }
        registry = _make_registry(groups=groups)
        bindings = _make_bindings(registry)
        # "codex" matches agent-b only → returns it regardless of sender
        agent = hub.resolve_agent(registry, bindings, "codex", sender="agent-a")
        self.assertEqual(agent["name"], "agent-b")

    def test_fuzzy_multiple_same_group_raises(self):
        """Multiple same-group matches should raise with disambiguation."""
        groups = {
            "alpha": {"description": "", "members": ["agent-a", "agent-b", "agent-c"]},
        }
        registry = _make_registry(groups=groups)
        bindings = _make_bindings(registry)
        # "agent" matches a, b, c. Sender=agent-a → exclude self → b, c in alpha → ambiguous
        with self.assertRaises(KeyError) as ctx:
            hub.resolve_agent(registry, bindings, "agent", sender="agent-a")
        self.assertIn("Multiple agents", str(ctx.exception))
        self.assertIn("agent-b", str(ctx.exception))
        self.assertIn("agent-c", str(ctx.exception))

    def test_no_match_raises(self):
        registry = _make_registry(groups={})
        bindings = _make_bindings(registry)
        with self.assertRaises(KeyError):
            hub.resolve_agent(registry, bindings, "nonexistent")

    def test_no_sender_no_group_falls_through(self):
        """Without sender, multiple matches raise generic disambiguation."""
        registry = _make_registry(groups={})
        bindings = _make_bindings(registry)
        with self.assertRaises(KeyError) as ctx:
            hub.resolve_agent(registry, bindings, "agent")
        self.assertIn("Multiple agents", str(ctx.exception))
