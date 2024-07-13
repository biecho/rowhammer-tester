# Copyright (c) 2022 Antmicro <www.antmicro.com>
# SPDX-License-Identifier: BSD-2-Clause

import re
import copy
import unittest
from typing import Mapping
from functools import partial
from collections import defaultdict

from migen import *

from litedram.phy.ddr5.simphy import DDR5SimPHY
from litedram.phy.ddr5 import simsoc
from litedram.phy.sim_utils import SimLogger
from litedram.phy.utils import Serializer, Deserializer

import test.phy_common
from test.phy_common import DFISequencer, PadChecker


class VerilatorDDR5Tests(unittest.TestCase):
    ALLOWED = []

    def check_logs(self, logs):
        memory_init = False
        for line in logs.splitlines():
            if "Switching SDRAM to software control." in line:
                memory_init = True

            match = SimLogger.LOG_PATTERN.match(line)
            if memory_init and match and match.group("level") in ["WARN", "ERROR"]:
                allowed = any(
                    lvl == match.group("level") and msg in match.group("msg")
                    for lvl, msg in self.ALLOWED
                )
                self.assertTrue(allowed, msg=match.group(0))

    def run_test(self, args, **kwargs):
        import pexpect

        command = ["python3", simsoc.__file__, *args]
        timeout = 6 * 60 * 60  # give more than enough time for CI

        p = pexpect.spawn(" ".join(command), timeout=timeout, **kwargs)

        # Useful for debugging: increase the size of the buffer printed to standard output upon fail.
        # p.str_last_chars = 2000

        res = p.expect(["Memtest OK", "Memtest KO"])
        self.assertEqual(res, 0, msg="{}\nGot '{}'".format(p.before.decode(), p.after.decode()))

        self.check_logs(p.before.decode())

    def test_ddr5_sim_dq_dqs_ratio_4_with_sub_channels_false_modules_per_rank_1(self):
        # Test simulation with regular delays, intermediate serialization stage,
        # refresh and with L2 cache (masked write doesn't work for x4)
        self.run_test([
            "--finish-after-memtest", "--log-level", "warn",
            "--output-dir", "build/test_ddr5_sim_dq_dqs_ratio_4",
            "--l2-size", "256",
            "--dq-dqs-ratio", "4",
        ])

    def test_ddr5_sim_dq_dqs_ratio_8_with_sub_channels_false_modules_per_rank_1_with_dm(self):
        # Test simulation with regular delays, intermediate serialization stage,
        # refresh and no L2 cache (masked write must work)
        self.run_test([
            "--finish-after-memtest", "--log-level", "warn",
            "--output-dir", "build/test_ddr5_sim_dq_dqs_ratio_8_with_dm",
            "--l2-size", "256",
            "--dq-dqs-ratio", "8",
        ])

    def test_ddr5_sim_dq_dqs_ratio_8_with_sub_channels_false_modules_per_rank_1_without_dm(self):
        # Test simulation with regular delays, intermediate serialization stage,
        # refresh and no L2 cache (masked write must work)
        self.run_test([
            "--finish-after-memtest", "--log-level", "warn",
            "--output-dir", "build/test_ddr5_sim_dq_dqs_ratio_8_without_dm",
            "--l2-size", "256",
            "--dq-dqs-ratio", "8",
            "--no-masked-write",
        ])

    def test_ddr5_sim_dq_dqs_ratio_4_with_sub_channels_true_modules_per_rank_1(self):
        # Test simulation with regular delays, intermediate serialization stage,
        # refresh and with L2 cache (masked write doesn't work for x4)
        self.run_test([
            "--finish-after-memtest", "--log-level", "warn",
            "--output-dir", "build/test_ddr5_sim_dq_dqs_ratio_4_with_sub_channels",
            "--l2-size", "256",
            "--dq-dqs-ratio", "4",
            "--with-sub-channels",
        ])

    def test_ddr5_sim_dq_dqs_ratio_8_with_sub_channels_true_modules_per_rank_1_with_dm(self):
        # Test simulation with regular delays, intermediate serialization stage,
        # refresh and no L2 cache (masked write must work)
        self.run_test([
            "--finish-after-memtest", "--log-level", "warn",
            "--output-dir", "build/test_ddr5_sim_dq_dqs_ratio_8_with_sub_channels_with_dm",
            "--l2-size", "256",
            "--dq-dqs-ratio", "8",
            "--with-sub-channels",
        ])

    def test_ddr5_sim_dq_dqs_ratio_8_with_sub_channels_true_modules_per_rank_1_without_dm(self):
        # Test simulation with regular delays, intermediate serialization stage,
        # refresh and no L2 cache (masked write must work)
        self.run_test([
            "--finish-after-memtest", "--log-level", "warn",
            "--output-dir", "build/test_ddr5_sim_dq_dqs_ratio_8_with_sub_channels_without_dm",
            "--l2-size", "256",
            "--dq-dqs-ratio", "8",
            "--with-sub-channels",
            "--no-masked-write",
        ])

    def test_ddr5_sim_dq_dqs_ratio_4_with_sub_channels_false_modules_per_rank_2(self):
        # Test simulation with regular delays, intermediate serialization stage,
        # refresh and with L2 cache (masked write doesn't work for x4)
        self.run_test([
            "--finish-after-memtest", "--log-level", "warn",
            "--output-dir", "build/test_ddr5_sim_dq_dqs_ratio_4",
            "--l2-size", "256",
            "--dq-dqs-ratio", "4",
            "--modules-in-rank", "2",
        ])

    def test_ddr5_sim_dq_dqs_ratio_8_with_sub_channels_false_modules_per_rank_2_with_dm(self):
        # Test simulation with regular delays, intermediate serialization stage,
        # refresh and no L2 cache (masked write must work)
        self.run_test([
            "--finish-after-memtest", "--log-level", "warn",
            "--output-dir", "build/test_ddr5_sim_dq_dqs_ratio_8_with_dm",
            "--l2-size", "256",
            "--dq-dqs-ratio", "8",
            "--modules-in-rank", "2",
        ])

    def test_ddr5_sim_dq_dqs_ratio_8_with_sub_channels_false_modules_per_rank_2_without_dm(self):
        # Test simulation with regular delays, intermediate serialization stage,
        # refresh and no L2 cache (masked write must work)
        self.run_test([
            "--finish-after-memtest", "--log-level", "warn",
            "--output-dir", "build/test_ddr5_sim_dq_dqs_ratio_8_without_dm",
            "--l2-size", "256",
            "--dq-dqs-ratio", "8",
            "--modules-in-rank", "2",
            "--no-masked-write",
        ])

    def test_ddr5_sim_dq_dqs_ratio_4_with_sub_channels_true_modules_per_rank_2(self):
        # Test simulation with regular delays, intermediate serialization stage,
        # refresh and with L2 cache (masked write doesn't work for x4)
        self.run_test([
            "--finish-after-memtest", "--log-level", "warn",
            "--output-dir", "build/test_ddr5_sim_dq_dqs_ratio_4_with_sub_channels",
            "--l2-size", "256",
            "--dq-dqs-ratio", "4",
            "--with-sub-channels",
            "--modules-in-rank", "2",
        ])

    def test_ddr5_sim_dq_dqs_ratio_8_with_sub_channels_true_modules_per_rank_2_with_dm(self):
        # Test simulation with regular delays, intermediate serialization stage,
        # refresh and no L2 cache (masked write must work)
        self.run_test([
            "--finish-after-memtest", "--log-level", "warn",
            "--output-dir", "build/test_ddr5_sim_dq_dqs_ratio_8_with_sub_channels_with_dm",
            "--l2-size", "256",
            "--dq-dqs-ratio", "8",
            "--with-sub-channels",
            "--modules-in-rank", "2",
        ])

    def test_ddr5_sim_dq_dqs_ratio_8_with_sub_channels_true_modules_per_rank_2_without_dm(self):
        # Test simulation with regular delays, intermediate serialization stage,
        # refresh and no L2 cache (masked write must work)
        self.run_test([
            "--finish-after-memtest", "--log-level", "warn",
            "--output-dir", "build/test_ddr5_sim_dq_dqs_ratio_8_with_sub_channels_without_dm",
            "--l2-size", "256",
            "--dq-dqs-ratio", "8",
            "--with-sub-channels",
            "--modules-in-rank", "2",
            "--no-masked-write",
        ])
