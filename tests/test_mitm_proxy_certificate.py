import unittest
import subprocess
from unittest.mock import Mock, call, patch

from backend import mitm_proxy


class MitmProxyCertificateTests(unittest.TestCase):
    @unittest.skipUnless(mitm_proxy.sys.platform == 'win32', 'Windows console regression')
    def test_status_polling_never_creates_console_windows(self):
        from flask import Flask
        from backend import channels

        app = Flask(__name__)
        app.register_blueprint(channels.channels_bp)
        manager = Mock(running=False, port=5202, last_error='')
        with (
            patch.object(mitm_proxy.ProxyManager, 'get_instance', return_value=manager),
            patch.object(mitm_proxy, '_current_ca_thumbprint', return_value='AABBCCDDEEFF'),
            patch.object(mitm_proxy.subprocess, 'run', return_value=Mock(returncode=0)) as run,
        ):
            for _ in range(3):
                response = app.test_client().get('/api/channels/proxy/status')
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.get_json()['cert_installed'])

        self.assertEqual(run.call_count, 3)
        for invocation in run.call_args_list:
            self.assertTrue(
                invocation.kwargs.get('creationflags', 0) & subprocess.CREATE_NO_WINDOW,
                'Each 3-second status poll launches certutil with a visible console in the windowless EXE',
            )

    def test_trust_check_uses_current_ca_thumbprint_instead_of_shared_name(self):
        not_found = Mock(returncode=1)
        with (
            patch.object(mitm_proxy.sys, "platform", "win32"),
            patch.object(
                mitm_proxy,
                "_current_ca_thumbprint",
                return_value="AABBCCDDEEFF",
                create=True,
            ),
            patch.object(mitm_proxy, "_run_certutil", return_value=not_found) as run,
        ):
            self.assertFalse(mitm_proxy.check_cert_trusted())

        self.assertEqual(
            run.call_args_list,
            [
                call(["-verifystore", "-user", "root", "AABBCCDDEEFF"]),
                call(["-verifystore", "root", "AABBCCDDEEFF"]),
            ],
        )


if __name__ == "__main__":
    unittest.main()
