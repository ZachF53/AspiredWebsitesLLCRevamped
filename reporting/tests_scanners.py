import subprocess
from unittest.mock import patch

from django.test import SimpleTestCase

from reporting.scanners import _classify_nikto_msg, run_nmap_scan


class NiktoClassifierTests(SimpleTestCase):
    """
    Regression coverage for the "uncommon header" misclassification —
    Nikto flags any header it doesn't recognize as "uncommon", including
    headers that are best-practice security controls. Before the fix,
    'X-XSS-Protection' was scored CRITICAL purely because its own name
    contains the substring 'xss', and the other good headers were scored
    MEDIUM off the bare keyword 'header'.
    """

    def test_known_security_headers_are_informational(self):
        cases = [
            "Uncommon header 'x-xss-protection' found, with contents: "
            "1; mode=block",
            "Uncommon header 'referrer-policy' found, with contents: "
            "strict-origin-when-cross-origin",
            "Uncommon header 'x-content-type-options' found, with "
            "contents: nosniff",
            "Uncommon header 'x-frame-options' found, with contents: "
            "SAMEORIGIN",
            "Uncommon header 'strict-transport-security' found",
            "Uncommon header 'content-security-policy' found",
        ]
        for msg in cases:
            with self.subTest(msg=msg):
                self.assertEqual(_classify_nikto_msg(msg), 'info')

    def test_unrecognized_uncommon_header_still_falls_through(self):
        # A header that isn't in the known-good allowlist should still
        # hit the generic keyword scan (the bare word 'header' -> medium),
        # not silently become info too.
        msg = "Uncommon header 'x-totally-made-up-header' found"
        self.assertEqual(_classify_nikto_msg(msg), 'medium')

    def test_genuine_xss_finding_still_critical(self):
        # Must not overcorrect — an actual XSS finding (not a header
        # name) should still classify as critical.
        msg = 'Cross Site Scripting (XSS) vulnerability found in parameter'
        self.assertEqual(_classify_nikto_msg(msg), 'critical')


_SAMPLE_NMAP_XML = """<?xml version="1.0"?>
<nmaprun>
  <host>
    <status state="up"/>
    <port protocol="tcp" portid="22">
      <state state="open"/>
      <service name="ssh" product="OpenSSH" version="8.9"/>
    </port>
    <port protocol="tcp" portid="6379">
      <state state="open"/>
      <service name="redis"/>
    </port>
  </host>
</nmaprun>
"""


class NmapRawOutputTests(SimpleTestCase):
    """
    nmap writes XML to a tempfile (passed via -oX) so its normal
    human-readable verbose report goes to stdout untouched — that's
    what the admin-only "Raw scan output" accordion displays. Piping
    -oX to stdout directly (the old approach) would replace that
    verbose text with raw XML instead.
    """

    def test_verbose_stdout_captured_and_xml_parsed(self):
        verbose_text = (
            'Starting Nmap 7.94 ( https://nmap.org )\n'
            'Nmap scan report for 143.244.155.250\n'
            'PORT     STATE SERVICE VERSION\n'
            '22/tcp   open  ssh     OpenSSH 8.9\n'
            '6379/tcp open  redis\n'
        )

        def fake_run(cmd, **kwargs):
            xml_path = cmd[cmd.index('-oX') + 1]
            with open(xml_path, 'w') as fh:
                fh.write(_SAMPLE_NMAP_XML)
            return subprocess.CompletedProcess(
                cmd, returncode=0, stdout=verbose_text, stderr='')

        with patch('reporting.scanners.subprocess.run', side_effect=fake_run):
            result = run_nmap_scan('143.244.155.250')

        self.assertEqual(result['raw_output'], verbose_text)
        self.assertEqual(len(result['ports']), 2)
        # Redis on 6379 with no auth is a baked-in critical finding.
        self.assertEqual(len(result['findings']), 1)
        self.assertEqual(result['findings'][0]['severity'], 'critical')
