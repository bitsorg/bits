# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""credential_free_env: what a package's own scripts get where credentials are."""

import os
import unittest
from unittest import mock

from bits_helpers.cmd import credential_free_env


class CredentialFreeEnvTest(unittest.TestCase):
    def test_credentials_go_the_rest_stays(self):
        gone = {"PREPUB_API_TOKEN": "1", "CI_JOB_TOKEN": "1", "BITS_ORCH_ID_TOKEN": "1", "GITLAB_TOKEN": "1",
                "BITS_FORGE_TOKEN": "1", "CI_REGISTRY_PASSWORD": "1", "CI_JOB_JWT_V2": "1", "BITS_SIGN_KEY": "1",
                "AWS_ACCESS_KEY_ID": "1", "AWS_SECRET_ACCESS_KEY": "1", "BITS_S3_X_SESSION_TOKEN": "1",
                "MY_secret": "1", "GPG_PASSPHRASE": "1", "GOOGLE_APPLICATION_CREDENTIALS": "1",
                "DOCKER_AUTH_CONFIG": "1", "CI_REPOSITORY_URL": "https://gitlab-ci-token:T@gitlab.cern.ch/x.git",
                "SSH_AUTH_SOCK": "/tmp/a", "SSH_AGENT_PID": "1", "GPG_AGENT_INFO": "/g", "GIT_ASKPASS": "/p",
                "SSH_ASKPASS": "/p", "NETRC": "/n", "KUBECONFIG": "/k", "KRB5CCNAME": "FILE:/tmp/k",
                "X509_USER_PROXY": "/x", "X509_USER_CERT": "/c", "X509_USER_KEY": "/k",
                "DOCKER_CONFIG": "/d", "AWS_CONFIG_FILE": "/a", "AWS_SHARED_CREDENTIALS_FILE": "/s"}
        kept = {"PATH": "/bin", "HOME": "/h", "INSTALL_BASE": "/cvmfs/x", "BITS_CVMFS_TEMPLATES": "{}",
                "KEYBOARD": "us", "LC_ALL": "C", "CI_PROJECT_URL": "https://gitlab.cern.ch/x",
                "MAILTO": "a@b.ch", "SSH_CONNECTION": "1 2 3 4", "X509_CERT_DIR": "/etc/grid-security"}
        with mock.patch.dict(os.environ, dict(gone, **kept), clear=True):
            env = credential_free_env()
        self.assertEqual(env, kept)


if __name__ == "__main__":
    unittest.main()
