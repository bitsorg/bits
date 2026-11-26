# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""--container-engine podman: bits runs its containers with podman instead of docker."""

import os
import types
import unittest
from unittest import mock

from bits_helpers import container_engine
from bits_helpers.cmd import DockerRunner
from bits_helpers.sandbox import wrap_build_command


class EngineTest(unittest.TestCase):
  def setUp(self):
    env = mock.patch.dict(os.environ)
    env.start()
    self.addCleanup(env.stop)
    os.environ.pop(container_engine.VARIABLE, None)

  def test_docker_unless_chosen(self):
    self.assertEqual(container_engine.engine(), "docker")
    self.assertEqual(container_engine.choose(None), "docker")
    self.assertEqual(container_engine.choose("podman"), "podman")
    self.assertEqual(os.environ[container_engine.VARIABLE], "podman")   # for what bits starts

  def test_another_is_refused(self):
    os.environ[container_engine.VARIABLE] = "nerdctl"
    with self.assertRaises(SystemExit):
      container_engine.engine()

  @mock.patch("bits_helpers.cmd.getoutput", return_value="cid\n")
  @mock.patch("bits_helpers.cmd.getstatusoutput")
  def test_the_helper_container(self, run, _out):
    container_engine.choose("podman")
    with DockerRunner("image", []) as inside:
      self.assertEqual(_out.call_args[0][0][:2], ["podman", "run"])
      inside("true")
      self.assertEqual(run.call_args[0][0][:2], ["podman", "container"])
    self.assertEqual(run.call_args[0][0], "podman container kill cid")

  def test_the_sandbox_keeps_the_engine(self):
    cmd = "podman run --rm --network=host img bash -ex /build.sh"
    opts = types.SimpleNamespace(sandbox="auto", sandboxImage=None, sandboxNetwork="on")
    got = wrap_build_command(cmd, {"package": "zlib"}, opts, workdir="/sw", docker_active=True)
    self.assertEqual(got, "podman run --network=none --rm img bash -ex /build.sh")

  @mock.patch("platform.system", return_value="Linux")
  @mock.patch("os.geteuid", return_value=1000)
  def test_podman_by_a_user_is_rootless(self, _uid, _sys):
    from bits_helpers.args import _is_rootless_podman
    container_engine.choose("podman")
    with mock.patch("bits_helpers.args.commands.run") as run:
      self.assertTrue(_is_rootless_podman())
    run.assert_not_called()                     # no `docker --version` asked

  def test_the_option_on_a_host_without_docker(self):
    from bits_helpers import args as bits_args
    a = types.SimpleNamespace(dockerImage="img", docker_extra_args="", docker=False,
                              architecture="x86_64-el9", dockerPlatform=None,
                              containerEngine="podman")
    which = lambda cmd: (0 if cmd == "which podman" else 1, "")
    with mock.patch.object(bits_args.commands, "getstatusoutput", which), \
         mock.patch.object(bits_args, "_is_rootless_podman", return_value=True), \
         mock.patch.object(bits_args, "_rootless_podman_controllers", return_value={"memory"}):
      bits_args._finalise_docker(a, mock.Mock(error=mock.Mock(side_effect=SystemExit)))
    self.assertEqual((os.environ[container_engine.VARIABLE], a.rootless_podman), ("podman", True))


if __name__ == "__main__":
  unittest.main()
