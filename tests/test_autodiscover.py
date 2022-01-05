from collections import namedtuple
import getpass
import glob
import sys
from types import MethodType
from unittest.mock import Mock, patch

import dns
import requests_mock

from exchangelib.account import Account
from exchangelib.credentials import Credentials, DELEGATE
import exchangelib.autodiscover.discovery
from exchangelib.autodiscover import close_connections, clear_cache, autodiscover_cache, AutodiscoverProtocol, \
    Autodiscovery, AutodiscoverCache
from exchangelib.autodiscover.cache import shelve_filename
from exchangelib.autodiscover.properties import Autodiscover
from exchangelib.configuration import Configuration
from exchangelib.errors import ErrorNonExistentMailbox, AutoDiscoverCircularRedirect, AutoDiscoverFailed
from exchangelib.protocol import FaultTolerance, FailFast
from exchangelib.transport import NTLM
from exchangelib.util import get_domain
from .common import EWSTest, get_random_string


class AutodiscoverTest(EWSTest):
    def setUp(self):
        super().setUp()

        # Enable retries, to make tests more robust
        Autodiscovery.INITIAL_RETRY_POLICY = FaultTolerance(max_wait=5)
        Autodiscovery.RETRY_WAIT = 5

        # Each test should start with a clean autodiscover cache
        clear_cache()

        # Some mocking helpers
        self.domain = get_domain(self.account.primary_smtp_address)
        self.dummy_ad_endpoint = f'https://{self.domain}/Autodiscover/Autodiscover.xml'
        self.dummy_ews_endpoint = 'https://expr.example.com/EWS/Exchange.asmx'
        self.dummy_ad_response = f'''\
<?xml version="1.0" encoding="utf-8"?>
<Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006">
    <Response xmlns="http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a">
        <User>
            <AutoDiscoverSMTPAddress>{self.account.primary_smtp_address}</AutoDiscoverSMTPAddress>
        </User>
        <Account>
            <AccountType>email</AccountType>
            <Action>settings</Action>
            <Protocol>
                <Type>EXPR</Type>
                <EwsUrl>{self.dummy_ews_endpoint}</EwsUrl>
            </Protocol>
        </Account>
    </Response>
</Autodiscover>'''.encode()
        self.dummy_ews_response = '''\
<?xml version='1.0' encoding='utf-8'?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">
  <s:Header>
    <h:ServerVersionInfo xmlns:h="http://schemas.microsoft.com/exchange/services/2006/types"
    MajorVersion="15" MinorVersion="1" MajorBuildNumber="1847" MinorBuildNumber="5" Version="V2017_07_11"/>
  </s:Header>
  <s:Body>
    <m:ResolveNamesResponse
    xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages"
    xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types">
      <m:ResponseMessages>
        <m:ResolveNamesResponseMessage ResponseClass="Success">
          <m:ResponseCode>NoError</m:ResponseCode>
          <m:ResolutionSet TotalItemsInView="0" IncludesLastItemInRange="true">
          </m:ResolutionSet>
        </m:ResolveNamesResponseMessage>
      </m:ResponseMessages>
    </m:ResolveNamesResponse>
  </s:Body>
</s:Envelope>
'''.encode()

    @requests_mock.mock(real_http=False)  # Just make sure we don't issue any real HTTP here
    def test_magic(self, m):
        # Just test we don't fail when calling repr() and str(). Insert a dummy cache entry for testing
        c = Credentials(get_random_string(8), get_random_string(8))
        autodiscover_cache[('example.com', c)] = AutodiscoverProtocol(config=Configuration(
            service_endpoint='https://example.com/Autodiscover/Autodiscover.xml',
            credentials=c,
            auth_type=NTLM,
            retry_policy=FailFast(),
        ))
        self.assertEqual(len(autodiscover_cache), 1)
        str(autodiscover_cache)
        repr(autodiscover_cache)
        for protocol in autodiscover_cache._protocols.values():
            str(protocol)
            repr(protocol)

    def test_autodiscover_empty_cache(self):
        # A live test of the entire process with an empty cache
        ad_response, protocol = exchangelib.autodiscover.discovery.discover(
            email=self.account.primary_smtp_address,
            credentials=self.account.protocol.credentials,
            retry_policy=self.retry_policy,
        )
        self.assertEqual(ad_response.autodiscover_smtp_address, self.account.primary_smtp_address)
        self.assertEqual(protocol.service_endpoint.lower(), self.account.protocol.service_endpoint.lower())
        self.assertEqual(protocol.version.build, self.account.protocol.version.build)

    def test_autodiscover_failure(self):
        # A live test that errors can be raised. Here, we try to aútodiscover a non-existing email address
        if not self.settings.get('autodiscover_server'):
            self.skipTest(f"Skipping {self.__class__.__name__} - no 'autodiscover_server' entry in settings.yml")
        # Autodiscovery may take a long time. Prime the cache with the autodiscover server from the config file
        ad_endpoint = f"https://{self.settings['autodiscover_server']}/Autodiscover/Autodiscover.xml"
        cache_key = (self.domain, self.account.protocol.credentials)
        autodiscover_cache[cache_key] = AutodiscoverProtocol(config=Configuration(
            service_endpoint=ad_endpoint,
            credentials=self.account.protocol.credentials,
            auth_type=NTLM,
            retry_policy=self.retry_policy,
        ))
        with self.assertRaises(ErrorNonExistentMailbox):
            exchangelib.autodiscover.discovery.discover(
                email='XXX.' + self.account.primary_smtp_address,
                credentials=self.account.protocol.credentials,
                retry_policy=self.retry_policy,
            )

    def test_failed_login_via_account(self):
        with self.assertRaises(AutoDiscoverFailed):
            Account(
                primary_smtp_address=self.account.primary_smtp_address,
                access_type=DELEGATE,
                credentials=Credentials(self.account.protocol.credentials.username, 'WRONG_PASSWORD'),
                autodiscover=True,
                locale='da_DK',
            )

    @requests_mock.mock(real_http=False)  # Just make sure we don't issue any real HTTP here
    def test_close_autodiscover_connections(self, m):
        # A live test that we can close TCP connections
        c = Credentials(get_random_string(8), get_random_string(8))
        autodiscover_cache[('example.com', c)] = AutodiscoverProtocol(config=Configuration(
            service_endpoint='https://example.com/Autodiscover/Autodiscover.xml',
            credentials=c,
            auth_type=NTLM,
            retry_policy=FailFast(),
        ))
        self.assertEqual(len(autodiscover_cache), 1)
        close_connections()

    @requests_mock.mock(real_http=False)  # Just make sure we don't issue any real HTTP here
    def test_autodiscover_direct_gc(self, m):
        # Test garbage collection of the autodiscover cache
        c = Credentials(get_random_string(8), get_random_string(8))
        autodiscover_cache[('example.com', c)] = AutodiscoverProtocol(config=Configuration(
            service_endpoint='https://example.com/Autodiscover/Autodiscover.xml',
            credentials=c,
            auth_type=NTLM,
            retry_policy=FailFast(),
        ))
        self.assertEqual(len(autodiscover_cache), 1)
        autodiscover_cache.__del__()

    @requests_mock.mock(real_http=False)
    def test_autodiscover_cache(self, m):
        # Mock the default endpoint that we test in step 1 of autodiscovery
        m.post(self.dummy_ad_endpoint, status_code=200, content=self.dummy_ad_response)
        # Also mock the EWS URL. We try to guess its auth method as part of autodiscovery
        m.post(self.dummy_ews_endpoint, status_code=200)
        discovery = Autodiscovery(
            email=self.account.primary_smtp_address,
            credentials=self.account.protocol.credentials,
            retry_policy=self.retry_policy,
        )
        # Not cached
        self.assertNotIn(discovery._cache_key, autodiscover_cache)
        discovery.discover()
        # Now it's cached
        self.assertIn(discovery._cache_key, autodiscover_cache)
        # Make sure the cache can be looked by value, not by id(). This is important for multi-threading/processing
        self.assertIn((
            self.account.primary_smtp_address.split('@')[1],
            Credentials(self.account.protocol.credentials.username, self.account.protocol.credentials.password),
            True
        ), autodiscover_cache)
        # Poison the cache with a failing autodiscover endpoint. discover() must handle this and rebuild the cache
        autodiscover_cache[discovery._cache_key] = AutodiscoverProtocol(config=Configuration(
            service_endpoint='https://example.com/Autodiscover/Autodiscover.xml',
            credentials=Credentials(get_random_string(8), get_random_string(8)),
            auth_type=NTLM,
            retry_policy=FailFast(),
        ))
        m.post('https://example.com/Autodiscover/Autodiscover.xml', status_code=404)
        discovery.discover()
        self.assertIn(discovery._cache_key, autodiscover_cache)

        # Make sure that the cache is actually used on the second call to discover()
        _orig = discovery._step_1

        def _mock(slf, *args, **kwargs):
            raise NotImplementedError()

        discovery._step_1 = MethodType(_mock, discovery)
        discovery.discover()

        # Fake that another thread added the cache entry into the persistent storage but we don't have it in our
        # in-memory cache. The cache should work anyway.
        autodiscover_cache._protocols.clear()
        discovery.discover()
        discovery._step_1 = _orig

        # Make sure we can delete cache entries even though we don't have it in our in-memory cache
        autodiscover_cache._protocols.clear()
        del autodiscover_cache[discovery._cache_key]
        # This should also work if the cache does not contain the entry anymore
        del autodiscover_cache[discovery._cache_key]

    @requests_mock.mock(real_http=False)  # Just make sure we don't issue any real HTTP here
    def test_corrupt_autodiscover_cache(self, m):
        # Insert a fake Protocol instance into the cache and test that we can recover
        key = (2, 'foo', 4)
        autodiscover_cache[key] = namedtuple('P', ['service_endpoint', 'auth_type', 'retry_policy'])(1, 'bar', 'baz')
        # Check that it exists. 'in' goes directly to the file
        self.assertTrue(key in autodiscover_cache)

        # Check that we can recover from a destroyed file
        for db_file in glob.glob(autodiscover_cache._storage_file + '*'):
            with open(db_file, 'w') as f:
                f.write('XXX')
        self.assertFalse(key in autodiscover_cache)

        # Check that we can recover from an empty file
        for db_file in glob.glob(autodiscover_cache._storage_file + '*'):
            with open(db_file, 'wb') as f:
                f.write(b'')
        self.assertFalse(key in autodiscover_cache)

    @requests_mock.mock(real_http=False)  # Just make sure we don't issue any real HTTP here
    def test_autodiscover_from_account(self, m):
        # Test that autodiscovery via account creation works
        # Mock the default endpoint that we test in step 1 of autodiscovery
        m.post(self.dummy_ad_endpoint, status_code=200, content=self.dummy_ad_response)
        # Also mock the EWS URL. We try to guess its auth method as part of autodiscovery
        m.post(self.dummy_ews_endpoint, status_code=200, content=self.dummy_ews_response)
        self.assertEqual(len(autodiscover_cache), 0)
        account = Account(
            primary_smtp_address=self.account.primary_smtp_address,
            config=Configuration(
                credentials=self.account.protocol.credentials,
                retry_policy=self.retry_policy,
            ),
            autodiscover=True,
            locale='da_DK',
        )
        self.assertEqual(account.primary_smtp_address, self.account.primary_smtp_address)
        self.assertEqual(account.protocol.service_endpoint.lower(), self.dummy_ews_endpoint.lower())
        # Make sure cache is full
        self.assertEqual(len(autodiscover_cache), 1)
        self.assertTrue((account.domain, self.account.protocol.credentials, True) in autodiscover_cache)
        # Test that autodiscover works with a full cache
        account = Account(
            primary_smtp_address=self.account.primary_smtp_address,
            config=Configuration(
                credentials=self.account.protocol.credentials,
                retry_policy=self.retry_policy,
            ),
            autodiscover=True,
            locale='da_DK',
        )
        self.assertEqual(account.primary_smtp_address, self.account.primary_smtp_address)
        # Test cache manipulation
        key = (account.domain, self.account.protocol.credentials, True)
        self.assertTrue(key in autodiscover_cache)
        del autodiscover_cache[key]
        self.assertFalse(key in autodiscover_cache)

    @requests_mock.mock(real_http=False)
    def test_autodiscover_redirect(self, m):
        # Test various aspects of autodiscover redirection. Mock all HTTP responses because we can't force a live server
        # to send us into the correct code paths.
        # Mock the default endpoint that we test in step 1 of autodiscovery
        m.post(self.dummy_ad_endpoint, status_code=200, content=self.dummy_ad_response)
        # Also mock the EWS URL. We try to guess its auth method as part of autodiscovery
        m.post(self.dummy_ews_endpoint, status_code=200)
        discovery = Autodiscovery(
            email=self.account.primary_smtp_address,
            credentials=self.account.protocol.credentials,
            retry_policy=self.retry_policy,
        )
        discovery.discover()

        # Make sure we discover a different return address
        m.post(self.dummy_ad_endpoint, status_code=200, content=b'''\
<?xml version="1.0" encoding="utf-8"?>
<Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006">
    <Response xmlns="http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a">
        <User>
            <AutoDiscoverSMTPAddress>john@example.com</AutoDiscoverSMTPAddress>
        </User>
        <Account>
            <AccountType>email</AccountType>
            <Action>settings</Action>
            <Protocol>
                <Type>EXPR</Type>
                <EwsUrl>https://expr.example.com/EWS/Exchange.asmx</EwsUrl>
            </Protocol>
        </Account>
    </Response>
</Autodiscover>''')
        # Also mock the EWS URL. We try to guess its auth method as part of autodiscovery
        m.post('https://expr.example.com/EWS/Exchange.asmx', status_code=200)
        ad_response, _ = discovery.discover()
        self.assertEqual(ad_response.autodiscover_smtp_address, 'john@example.com')

        # Make sure we discover an address redirect to the same domain. We have to mock the same URL with two different
        # responses. We do that with a response list.
        redirect_addr_content = f'''\
<?xml version="1.0" encoding="utf-8"?>
<Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006">
    <Response xmlns="http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a">
        <Account>
            <Action>redirectAddr</Action>
            <RedirectAddr>redirect_me@{self.domain}</RedirectAddr>
        </Account>
    </Response>
</Autodiscover>'''.encode()
        settings_content = f'''\
<?xml version="1.0" encoding="utf-8"?>
<Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006">
    <Response xmlns="http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a">
        <User>
            <AutoDiscoverSMTPAddress>redirected@{self.domain}</AutoDiscoverSMTPAddress>
        </User>
        <Account>
            <AccountType>email</AccountType>
            <Action>settings</Action>
            <Protocol>
                <Type>EXPR</Type>
                <EwsUrl>https://redirected.{self.domain}/EWS/Exchange.asmx</EwsUrl>
            </Protocol>
        </Account>
    </Response>
</Autodiscover>'''.encode()
        # Also mock the EWS URL. We try to guess its auth method as part of autodiscovery
        m.post(f'https://redirected.{self.domain}/EWS/Exchange.asmx', status_code=200)

        m.post(self.dummy_ad_endpoint, [
            dict(status_code=200, content=redirect_addr_content),
            dict(status_code=200, content=settings_content),
        ])
        ad_response, _ = discovery.discover()
        self.assertEqual(ad_response.autodiscover_smtp_address, f'redirected@{self.domain}')
        self.assertEqual(ad_response.ews_url, f'https://redirected.{self.domain}/EWS/Exchange.asmx')

        # Test that we catch circular redirects on the same domain with a primed cache. Just mock the endpoint to
        # return the same redirect response on every request.
        self.assertEqual(len(autodiscover_cache), 1)
        m.post(self.dummy_ad_endpoint, status_code=200, content=f'''\
<?xml version="1.0" encoding="utf-8"?>
<Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006">
    <Response xmlns="http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a">
        <Account>
            <Action>redirectAddr</Action>
            <RedirectAddr>foo@{self.domain}</RedirectAddr>
        </Account>
    </Response>
</Autodiscover>'''.encode())
        self.assertEqual(len(autodiscover_cache), 1)
        with self.assertRaises(AutoDiscoverCircularRedirect):
            discovery.discover()

        # Test that we also catch circular redirects when cache is empty
        clear_cache()
        self.assertEqual(len(autodiscover_cache), 0)
        with self.assertRaises(AutoDiscoverCircularRedirect):
            discovery.discover()

        # Test that we can handle being asked to redirect to an address on a different domain
        # Don't use example.com to redirect - it does not resolve or answer on all ISPs
        m.post(self.dummy_ad_endpoint, status_code=200, content=b'''\
<?xml version="1.0" encoding="utf-8"?>
<Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006">
    <Response xmlns="http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a">
        <Account>
            <Action>redirectAddr</Action>
            <RedirectAddr>john@httpbin.org</RedirectAddr>
        </Account>
    </Response>
</Autodiscover>''')
        m.post('https://httpbin.org/Autodiscover/Autodiscover.xml', status_code=200, content=b'''\
<?xml version="1.0" encoding="utf-8"?>
<Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006">
    <Response xmlns="http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a">
        <User>
            <AutoDiscoverSMTPAddress>john@redirected.httpbin.org</AutoDiscoverSMTPAddress>
        </User>
        <Account>
            <AccountType>email</AccountType>
            <Action>settings</Action>
            <Protocol>
                <Type>EXPR</Type>
                <EwsUrl>https://httpbin.org/EWS/Exchange.asmx</EwsUrl>
            </Protocol>
        </Account>
    </Response>
</Autodiscover>''')
        # Also mock the EWS URL. We try to guess its auth method as part of autodiscovery
        m.post('https://httpbin.org/EWS/Exchange.asmx', status_code=200)
        ad_response, _ = discovery.discover()
        self.assertEqual(ad_response.autodiscover_smtp_address, 'john@redirected.httpbin.org')
        self.assertEqual(ad_response.ews_url, 'https://httpbin.org/EWS/Exchange.asmx')

    def test_get_srv_records(self):
        from exchangelib.autodiscover.discovery import SrvRecord
        ad = Autodiscovery('foo@example.com')
        # Unknown domain
        self.assertEqual(ad._get_srv_records('example.XXXXX'), [])
        # No SRV record
        self.assertEqual(ad._get_srv_records('example.com'), [])
        # Finding a real server that has a correct SRV record is not easy. Mock it
        _orig = dns.resolver.Resolver

        class _Mock1:
            @staticmethod
            def resolve(hostname, cat):
                class A:
                    @staticmethod
                    def to_text():
                        # Return a valid record
                        return '1 2 3 example.com.'
                return [A()]

        dns.resolver.Resolver = _Mock1
        del ad.resolver
        # Test a valid record
        self.assertEqual(
            ad._get_srv_records('example.com.'),
            [SrvRecord(priority=1, weight=2, port=3, srv='example.com')]
        )

        class _Mock2:
            @staticmethod
            def resolve(hostname, cat):
                class A:
                    @staticmethod
                    def to_text():
                        # Return malformed data
                        return 'XXXXXXX'
                return [A()]

        dns.resolver.Resolver = _Mock2
        del ad.resolver
        # Test an invalid record
        self.assertEqual(ad._get_srv_records('example.com'), [])
        dns.resolver.Resolver = _orig
        del ad.resolver

    def test_select_srv_host(self):
        from exchangelib.autodiscover.discovery import _select_srv_host, SrvRecord
        with self.assertRaises(ValueError):
            # Empty list
            _select_srv_host([])
        with self.assertRaises(ValueError):
            # No records with TLS port
            _select_srv_host([SrvRecord(priority=1, weight=2, port=3, srv='example.com')])
        # One record
        self.assertEqual(
            _select_srv_host([SrvRecord(priority=1, weight=2, port=443, srv='example.com')]),
            'example.com'
        )
        # Highest priority record
        self.assertEqual(
            _select_srv_host([
                SrvRecord(priority=10, weight=2, port=443, srv='10.example.com'),
                SrvRecord(priority=1, weight=2, port=443, srv='1.example.com'),
            ]),
            '10.example.com'
        )
        # Highest priority record no matter how it's sorted
        self.assertEqual(
            _select_srv_host([
                SrvRecord(priority=1, weight=2, port=443, srv='1.example.com'),
                SrvRecord(priority=10, weight=2, port=443, srv='10.example.com'),
            ]),
            '10.example.com'
        )

    def test_parse_response(self):
        # Test parsing of various XML responses
        with self.assertRaises(ValueError):
            Autodiscover.from_bytes(b'XXX')  # Invalid response

        xml = b'''<?xml version="1.0" encoding="utf-8"?><foo>bar</foo>'''
        with self.assertRaises(ValueError):
            Autodiscover.from_bytes(xml)  # Invalid XML response

        # Redirect to different email address
        xml = b'''\
<?xml version="1.0" encoding="utf-8"?>
<Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006">
    <Response xmlns="http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a">
        <User>
            <AutoDiscoverSMTPAddress>john@demo.affect-it.dk</AutoDiscoverSMTPAddress>
        </User>
        <Account>
            <Action>redirectAddr</Action>
            <RedirectAddr>foo@example.com</RedirectAddr>
        </Account>
    </Response>
</Autodiscover>'''
        self.assertEqual(Autodiscover.from_bytes(xml).response.redirect_address, 'foo@example.com')

        # Redirect to different URL
        xml = b'''\
<?xml version="1.0" encoding="utf-8"?>
<Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006">
    <Response xmlns="http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a">
        <User>
            <AutoDiscoverSMTPAddress>john@demo.affect-it.dk</AutoDiscoverSMTPAddress>
        </User>
        <Account>
            <Action>redirectUrl</Action>
            <RedirectURL>https://example.com/foo.asmx</RedirectURL>
        </Account>
    </Response>
</Autodiscover>'''
        self.assertEqual(Autodiscover.from_bytes(xml).response.redirect_url, 'https://example.com/foo.asmx')

        # Select EXPR if it's there, and there are multiple available
        xml = b'''\
<?xml version="1.0" encoding="utf-8"?>
<Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006">
    <Response xmlns="http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a">
        <User>
            <AutoDiscoverSMTPAddress>john@demo.affect-it.dk</AutoDiscoverSMTPAddress>
        </User>
        <Account>
            <AccountType>email</AccountType>
            <Action>settings</Action>
            <Protocol>
                <Type>EXCH</Type>
                <EwsUrl>https://exch.example.com/EWS/Exchange.asmx</EwsUrl>
            </Protocol>
            <Protocol>
                <Type>EXPR</Type>
                <EwsUrl>https://expr.example.com/EWS/Exchange.asmx</EwsUrl>
            </Protocol>
        </Account>
    </Response>
</Autodiscover>'''
        self.assertEqual(
            Autodiscover.from_bytes(xml).response.ews_url,
            'https://expr.example.com/EWS/Exchange.asmx'
        )

        # Select EXPR if EXPR is unavailable
        xml = b'''\
<?xml version="1.0" encoding="utf-8"?>
<Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006">
    <Response xmlns="http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a">
        <User>
            <AutoDiscoverSMTPAddress>john@demo.affect-it.dk</AutoDiscoverSMTPAddress>
        </User>
        <Account>
            <AccountType>email</AccountType>
            <Action>settings</Action>
            <Protocol>
                <Type>EXCH</Type>
                <EwsUrl>https://exch.example.com/EWS/Exchange.asmx</EwsUrl>
            </Protocol>
        </Account>
    </Response>
</Autodiscover>'''
        self.assertEqual(
            Autodiscover.from_bytes(xml).response.ews_url,
            'https://exch.example.com/EWS/Exchange.asmx'
        )

        # Fail if neither EXPR nor EXPR are unavailable
        xml = b'''\
<?xml version="1.0" encoding="utf-8"?>
<Autodiscover xmlns="http://schemas.microsoft.com/exchange/autodiscover/responseschema/2006">
    <Response xmlns="http://schemas.microsoft.com/exchange/autodiscover/outlook/responseschema/2006a">
        <User>
            <AutoDiscoverSMTPAddress>john@demo.affect-it.dk</AutoDiscoverSMTPAddress>
        </User>
        <Account>
            <AccountType>email</AccountType>
            <Action>settings</Action>
            <Protocol>
                <Type>XXX</Type>
                <EwsUrl>https://xxx.example.com/EWS/Exchange.asmx</EwsUrl>
            </Protocol>
        </Account>
    </Response>
</Autodiscover>'''
        with self.assertRaises(ValueError):
            Autodiscover.from_bytes(xml).response.ews_url

    def test_del_on_error(self):
        # Test that __del__ can handle exceptions on close()
        cache = AutodiscoverCache()
        tmp = AutodiscoverCache.close
        try:
            AutodiscoverCache.close = Mock(side_effect=Exception('XXX'))
            with self.assertRaises(Exception):
                cache.close()
            del cache
        finally:
            AutodiscoverCache.close = tmp

    def test_shelve_filename(self):
        major, minor = sys.version_info[:2]
        self.assertEqual(shelve_filename(), f'exchangelib.2.cache.{getpass.getuser()}.py{major}{minor}')

    @patch('getpass.getuser', side_effect=KeyError())
    def test_shelve_filename_getuser_failure(self, m):
        # Test that shelve_filename can handle a failing getuser()
        major, minor = sys.version_info[:2]
        self.assertEqual(shelve_filename(), f'exchangelib.2.cache.exchangelib.py{major}{minor}')
