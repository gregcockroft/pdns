#!/usr/bin/env python2

import copy
import Queue
import os
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import unittest
import clientsubnetoption
import dns
import dns.message
import libnacl
import libnacl.utils

class DNSDistTest(unittest.TestCase):
    """
    Set up a dnsdist instance and responder threads.
    Queries sent to dnsdist are relayed to the responder threads,
    who reply with the response provided by the tests themselves
    on a queue. Responder threads also queue the queries received
    from dnsdist on a separate queue, allowing the tests to check
    that the queries sent from dnsdist were as expected.
    """
    _dnsDistPort = 5340
    _dnsDistListeningAddr = "127.0.0.1"
    _testServerPort = 5350
    _toResponderQueue = Queue.Queue()
    _fromResponderQueue = Queue.Queue()
    _queueTimeout = 1
    _dnsdistStartupDelay = 2.0
    _dnsdist = None
    _responsesCounter = {}
    _shutUp = True
    _config_template = """
    """
    _config_params = ['_testServerPort']
    _acl = ['127.0.0.1/32']
    _consolePort = 5199
    _consoleKey = None

    @classmethod
    def startResponders(cls):
        print("Launching responders..")

        cls._UDPResponder = threading.Thread(name='UDP Responder', target=cls.UDPResponder, args=[cls._testServerPort, cls._toResponderQueue, cls._fromResponderQueue])
        cls._UDPResponder.setDaemon(True)
        cls._UDPResponder.start()
        cls._TCPResponder = threading.Thread(name='TCP Responder', target=cls.TCPResponder, args=[cls._testServerPort, cls._toResponderQueue, cls._fromResponderQueue])
        cls._TCPResponder.setDaemon(True)
        cls._TCPResponder.start()

    @classmethod
    def startDNSDist(cls, shutUp=True):
        print("Launching dnsdist..")
        conffile = 'dnsdist_test.conf'
        params = tuple([getattr(cls, param) for param in cls._config_params])
        print(params)
        with open(conffile, 'w') as conf:
            conf.write("-- Autogenerated by dnsdisttests.py\n")
            conf.write(cls._config_template % params)

        dnsdistcmd = [os.environ['DNSDISTBIN'], '-C', conffile,
                      '-l', '%s:%d' % (cls._dnsDistListeningAddr, cls._dnsDistPort) ]
        for acl in cls._acl:
            dnsdistcmd.extend(['--acl', acl])
        print(' '.join(dnsdistcmd))

        if shutUp:
            with open(os.devnull, 'w') as fdDevNull:
                cls._dnsdist = subprocess.Popen(dnsdistcmd, close_fds=True, stdout=fdDevNull)
        else:
            cls._dnsdist = subprocess.Popen(dnsdistcmd, close_fds=True)

        if 'DNSDIST_FAST_TESTS' in os.environ:
            delay = 0.5
        else:
            delay = cls._dnsdistStartupDelay

        time.sleep(delay)

        if cls._dnsdist.poll() is not None:
            cls._dnsdist.kill()
            sys.exit(cls._dnsdist.returncode)

    @classmethod
    def setUpSockets(cls):
        print("Setting up UDP socket..")
        cls._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        cls._sock.settimeout(2.0)
        cls._sock.connect(("127.0.0.1", cls._dnsDistPort))

    @classmethod
    def setUpClass(cls):

        cls.startResponders()
        cls.startDNSDist(cls._shutUp)
        cls.setUpSockets()

        print("Launching tests..")

    @classmethod
    def tearDownClass(cls):
        if 'DNSDIST_FAST_TESTS' in os.environ:
            delay = 0.1
        else:
            delay = 1.0
        if cls._dnsdist:
            cls._dnsdist.terminate()
            if cls._dnsdist.poll() is None:
                time.sleep(delay)
                if cls._dnsdist.poll() is None:
                    cls._dnsdist.kill()
                cls._dnsdist.wait()

    @classmethod
    def _ResponderIncrementCounter(cls):
        if threading.currentThread().name in cls._responsesCounter:
            cls._responsesCounter[threading.currentThread().name] += 1
        else:
            cls._responsesCounter[threading.currentThread().name] = 1

    @classmethod
    def _getResponse(cls, request, fromQueue, toQueue):
        response = None
        if len(request.question) != 1:
            print("Skipping query with question count %d" % (len(request.question)))
            return None
        healthcheck = not str(request.question[0].name).endswith('tests.powerdns.com.')
        if not healthcheck:
            cls._ResponderIncrementCounter()
            if not fromQueue.empty():
                response = fromQueue.get(True, cls._queueTimeout)
                if response:
                    response = copy.copy(response)
                    response.id = request.id
                    toQueue.put(request, True, cls._queueTimeout)

        if not response:
            # unexpected query, or health check
            response = dns.message.make_response(request)

        return response

    @classmethod
    def UDPResponder(cls, port, fromQueue, toQueue, ignoreTrailing=False):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(("127.0.0.1", port))
        while True:
            data, addr = sock.recvfrom(4096)
            request = dns.message.from_wire(data, ignore_trailing=ignoreTrailing)
            response = cls._getResponse(request, fromQueue, toQueue)

            if not response:
                continue

            sock.settimeout(2.0)
            sock.sendto(response.to_wire(), addr)
            sock.settimeout(None)
        sock.close()

    @classmethod
    def TCPResponder(cls, port, fromQueue, toQueue, ignoreTrailing=False, multipleResponses=False):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except socket.error as e:
            print("Error binding in the TCP responder: %s" % str(e))
            sys.exit(1)

        sock.listen(100)
        while True:
            (conn, _) = sock.accept()
            conn.settimeout(2.0)
            data = conn.recv(2)
            (datalen,) = struct.unpack("!H", data)
            data = conn.recv(datalen)
            request = dns.message.from_wire(data, ignore_trailing=ignoreTrailing)
            response = cls._getResponse(request, fromQueue, toQueue)

            if not response:
                conn.close()
                continue

            wire = response.to_wire()
            conn.send(struct.pack("!H", len(wire)))
            conn.send(wire)

            while multipleResponses:
                if fromQueue.empty():
                    break

                response = fromQueue.get(True, cls._queueTimeout)
                if not response:
                    break

                response = copy.copy(response)
                response.id = request.id
                wire = response.to_wire()
                try:
                    conn.send(struct.pack("!H", len(wire)))
                    conn.send(wire)
                except socket.error as e:
                    # some of the tests are going to close
                    # the connection on us, just deal with it
                    break

            conn.close()

        sock.close()

    @classmethod
    def sendUDPQuery(cls, query, response, useQueue=True, timeout=2.0, rawQuery=False):
        if useQueue:
            cls._toResponderQueue.put(response, True, timeout)

        if timeout:
            cls._sock.settimeout(timeout)

        try:
            if not rawQuery:
                query = query.to_wire()
            cls._sock.send(query)
            data = cls._sock.recv(4096)
        except socket.timeout:
            data = None
        finally:
            if timeout:
                cls._sock.settimeout(None)

        receivedQuery = None
        message = None
        if useQueue and not cls._fromResponderQueue.empty():
            receivedQuery = cls._fromResponderQueue.get(True, timeout)
        if data:
            message = dns.message.from_wire(data)
        return (receivedQuery, message)

    @classmethod
    def openTCPConnection(cls, timeout=None):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if timeout:
            sock.settimeout(timeout)

        sock.connect(("127.0.0.1", cls._dnsDistPort))
        return sock

    @classmethod
    def openTLSConnection(cls, port, serverName, caCert=None, timeout=None):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if timeout:
            sock.settimeout(timeout)

        # 2.7.9+
        if hasattr(ssl, 'create_default_context'):
            sslctx = ssl.create_default_context(cafile=caCert)
            sslsock = sslctx.wrap_socket(sock, server_hostname=serverName)
        else:
            sslsock = ssl.wrap_socket(sock, ca_certs=caCert, cert_reqs=ssl.CERT_REQUIRED)

        sslsock.connect(("127.0.0.1", port))
        return sslsock

    @classmethod
    def sendTCPQueryOverConnection(cls, sock, query, rawQuery=False, response=None, timeout=2.0):
        if not rawQuery:
            wire = query.to_wire()
        else:
            wire = query

        if response:
            cls._toResponderQueue.put(response, True, timeout)

        sock.send(struct.pack("!H", len(wire)))
        sock.send(wire)

    @classmethod
    def recvTCPResponseOverConnection(cls, sock, useQueue=False, timeout=2.0):
        message = None
        data = sock.recv(2)
        if data:
            (datalen,) = struct.unpack("!H", data)
            data = sock.recv(datalen)
            if data:
                message = dns.message.from_wire(data)

        if useQueue and not cls._fromResponderQueue.empty():
            receivedQuery = cls._fromResponderQueue.get(True, timeout)
            return (receivedQuery, message)
        else:
            return message

    @classmethod
    def sendTCPQuery(cls, query, response, useQueue=True, timeout=2.0, rawQuery=False):
        message = None
        if useQueue:
            cls._toResponderQueue.put(response, True, timeout)

        sock = cls.openTCPConnection(timeout)

        try:
            cls.sendTCPQueryOverConnection(sock, query, rawQuery)
            message = cls.recvTCPResponseOverConnection(sock)
        except socket.timeout as e:
            print("Timeout: %s" % (str(e)))
        except socket.error as e:
            print("Network error: %s" % (str(e)))
        finally:
            sock.close()

        receivedQuery = None
        if useQueue and not cls._fromResponderQueue.empty():
            receivedQuery = cls._fromResponderQueue.get(True, timeout)

        return (receivedQuery, message)

    @classmethod
    def sendTCPQueryWithMultipleResponses(cls, query, responses, useQueue=True, timeout=2.0, rawQuery=False):
        if useQueue:
            for response in responses:
                cls._toResponderQueue.put(response, True, timeout)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if timeout:
            sock.settimeout(timeout)

        sock.connect(("127.0.0.1", cls._dnsDistPort))
        messages = []

        try:
            if not rawQuery:
                wire = query.to_wire()
            else:
                wire = query

            sock.send(struct.pack("!H", len(wire)))
            sock.send(wire)
            while True:
                data = sock.recv(2)
                if not data:
                    break
                (datalen,) = struct.unpack("!H", data)
                data = sock.recv(datalen)
                messages.append(dns.message.from_wire(data))

        except socket.timeout as e:
            print("Timeout: %s" % (str(e)))
        except socket.error as e:
            print("Network error: %s" % (str(e)))
        finally:
            sock.close()

        receivedQuery = None
        if useQueue and not cls._fromResponderQueue.empty():
            receivedQuery = cls._fromResponderQueue.get(True, timeout)
        return (receivedQuery, messages)

    def setUp(self):
        # This function is called before every tests

        # Clear the responses counters
        for key in self._responsesCounter:
            self._responsesCounter[key] = 0

        # Make sure the queues are empty, in case
        # a previous test failed
        while not self._toResponderQueue.empty():
            self._toResponderQueue.get(False)

        while not self._fromResponderQueue.empty():
            self._fromResponderQueue.get(False)

    @classmethod
    def clearToResponderQueue(cls):
        while not cls._toResponderQueue.empty():
            cls._toResponderQueue.get(False)

    @classmethod
    def clearFromResponderQueue(cls):
        while not cls._fromResponderQueue.empty():
            cls._fromResponderQueue.get(False)

    @classmethod
    def clearResponderQueues(cls):
        cls.clearToResponderQueue()
        cls.clearFromResponderQueue()

    @staticmethod
    def generateConsoleKey():
        return libnacl.utils.salsa_key()

    @classmethod
    def _encryptConsole(cls, command, nonce):
        if cls._consoleKey is None:
            return command
        return libnacl.crypto_secretbox(command, nonce, cls._consoleKey)

    @classmethod
    def _decryptConsole(cls, command, nonce):
        if cls._consoleKey is None:
            return command
        return libnacl.crypto_secretbox_open(command, nonce, cls._consoleKey)

    @classmethod
    def sendConsoleCommand(cls, command, timeout=1.0):
        ourNonce = libnacl.utils.rand_nonce()
        theirNonce = None
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if timeout:
            sock.settimeout(timeout)

        sock.connect(("127.0.0.1", cls._consolePort))
        sock.send(ourNonce)
        theirNonce = sock.recv(len(ourNonce))
        if len(theirNonce) != len(ourNonce):
            print("Received a nonce of size %, expecting %, console command will not be sent!" % (len(theirNonce), len(ourNonce)))
            return None

        halfNonceSize = len(ourNonce) / 2
        readingNonce = ourNonce[0:halfNonceSize] + theirNonce[halfNonceSize:]
        writingNonce = theirNonce[0:halfNonceSize] + ourNonce[halfNonceSize:]
        msg = cls._encryptConsole(command, writingNonce)
        sock.send(struct.pack("!I", len(msg)))
        sock.send(msg)
        data = sock.recv(4)
        (responseLen,) = struct.unpack("!I", data)
        data = sock.recv(responseLen)
        response = cls._decryptConsole(data, readingNonce)
        return response

    def compareOptions(self, a, b):
        self.assertEquals(len(a), len(b))
        for idx in xrange(len(a)):
            self.assertEquals(a[idx], b[idx])

    def checkMessageNoEDNS(self, expected, received):
        self.assertEquals(expected, received)
        self.assertEquals(received.edns, -1)
        self.assertEquals(len(received.options), 0)

    def checkMessageEDNSWithoutECS(self, expected, received, withCookies=0):
        self.assertEquals(expected, received)
        self.assertEquals(received.edns, 0)
        self.assertEquals(len(received.options), withCookies)
        if withCookies:
            for option in received.options:
                self.assertEquals(option.otype, 10)

    def checkMessageEDNSWithECS(self, expected, received):
        self.assertEquals(expected, received)
        self.assertEquals(received.edns, 0)
        self.assertEquals(len(received.options), 1)
        self.assertEquals(received.options[0].otype, clientsubnetoption.ASSIGNED_OPTION_CODE)
        self.compareOptions(expected.options, received.options)

    def checkQueryEDNSWithECS(self, expected, received):
        self.checkMessageEDNSWithECS(expected, received)

    def checkResponseEDNSWithECS(self, expected, received):
        self.checkMessageEDNSWithECS(expected, received)

    def checkQueryEDNSWithoutECS(self, expected, received):
        self.checkMessageEDNSWithoutECS(expected, received)

    def checkResponseEDNSWithoutECS(self, expected, received, withCookies=0):
        self.checkMessageEDNSWithoutECS(expected, received, withCookies)

    def checkQueryNoEDNS(self, expected, received):
        self.checkMessageNoEDNS(expected, received)

    def checkResponseNoEDNS(self, expected, received):
        self.checkMessageNoEDNS(expected, received)
