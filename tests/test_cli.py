from __future__ import annotations

import unittest
from argparse import Namespace

from kanbanlan.cli import _project_number, _project_reference


class CliTests(unittest.TestCase):
    def test_project_url_is_parsed(self) -> None:
        args = Namespace(
            project_owner=None,
            project_number=None,
            project_url="https://github.com/orgs/paracord-clients/projects/2",
        )

        self.assertEqual(
            ("paracord-clients", 2),
            _project_reference(args, "repository-owner"),
        )

    def test_created_project_number_falls_back_to_url(self) -> None:
        self.assertEqual(
            17,
            _project_number({"url": "https://github.com/orgs/acme/projects/17"}),
        )
