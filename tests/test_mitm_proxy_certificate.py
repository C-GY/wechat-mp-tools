import unittest
from unittest.mock import Mock, call, patch

from backend import mitm_proxy


class MitmProxyCertificateTests(unittest.TestCase):
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
