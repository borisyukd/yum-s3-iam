#!/usr/bin/env python
# Copyright 2012, Julius Seporaitis
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import urllib2
import urlparse
import time
import hashlib
import hmac
import json

import yum
import yum.config
import yum.Errors
import yum.plugins

from yum.yumRepo import YumRepository

__author__ = "Julius Seporaitis"
__email__ = "julius@seporaitis.net"
__copyright__ = "Copyright 2012, Julius Seporaitis"
__license__ = "Apache 2.0"
__version__ = "1.0.3"


__all__ = ['requires_api_version', 'plugin_type', 'CONDUIT',
           'config_hook', 'prereposetup_hook']

requires_api_version = '2.5'
plugin_type = yum.plugins.TYPE_CORE
CONDUIT = None


def config_hook(conduit):
    yum.config.RepoConf.s3_enabled = yum.config.BoolOption(False)
    yum.config.RepoConf.key_id = yum.config.Option()
    yum.config.RepoConf.secret_key = yum.config.Option()
    yum.config.RepoConf.delegated_role = yum.config.Option()


def prereposetup_hook(conduit):
    """Plugin initialization hook. Setup the S3 repositories."""

    repos = conduit.getRepos()

    for repo in repos.listEnabled():
        if isinstance(repo, YumRepository) and repo.s3_enabled:
            new_repo = S3Repository(repo.id, repo.baseurl)
            new_repo.name = repo.name
            # new_repo.baseurl = repo.baseurl
            new_repo.mirrorlist = repo.mirrorlist
            new_repo.basecachedir = repo.basecachedir
            new_repo.gpgcheck = repo.gpgcheck
            new_repo.gpgkey = repo.gpgkey
            new_repo.key_id = repo.key_id
            new_repo.secret_key = repo.secret_key
            new_repo.proxy = repo.proxy
            new_repo.enablegroups = repo.enablegroups
            if hasattr(repo, 'priority'):
                new_repo.priority = repo.priority
            if hasattr(repo, 'base_persistdir'):
                new_repo.base_persistdir = repo.base_persistdir
            if hasattr(repo, 'metadata_expire'):
                new_repo.metadata_expire = repo.metadata_expire
            if hasattr(repo, 'skip_if_unavailable'):
                new_repo.skip_if_unavailable = repo.skip_if_unavailable

            repos.delete(repo.id)
            repos.add(new_repo)


class S3Repository(YumRepository):
    """Repository object for Amazon S3, using IAM Roles."""

    def __init__(self, repoid, baseurl):
        super(S3Repository, self).__init__(repoid)
        self.iamrole = None
        self.baseurl = baseurl
        self.grabber = None
        self.enable()

    @property
    def grabfunc(self):
        raise NotImplementedError("grabfunc called, when it shouldn't be!")

    @property
    def grab(self):
        if not self.grabber:
            self.grabber = S3Grabber(self)
            if self.key_id and self.secret_key:
                self.grabber.set_credentials(self.key_id, self.secret_key)
            elif self.delegated_role:
                self.grabber.get_instance_region()
                self.grabber.get_delegated_role_credentials(self.delegated_role)
            else:
                self.grabber.get_role()
                self.grabber.get_credentials()
        return self.grabber


class S3Grabber(object):

    def __init__(self, repo):
        """Initialize file grabber.
        Note: currently supports only single repo.baseurl. So in case of a list
              only the first item will be used.
        """
        if isinstance(repo, basestring):
            self.baseurl = repo
        else:
            if len(repo.baseurl) != 1:
                raise yum.plugins.PluginYumExit("s3iam: repository '%s' "
                                                "must have only one "
                                                "'baseurl' value" % repo.id)
            else:
                self.baseurl = repo.baseurl[0]
        # Ensure urljoin doesn't ignore base path:
        if not self.baseurl.endswith('/'):
            self.baseurl += '/'

    def get_role(self):
        """Read IAM role from AWS metadata store."""
        request = urllib2.Request(
            urlparse.urljoin(
                "http://169.254.169.254",
                "/latest/meta-data/iam/security-credentials/"
            ))

        response = None
        try:
            response = urllib2.urlopen(request)
            self.iamrole = (response.read())
        finally:
            if response:
                response.close()

    def get_credentials(self):
        """Read IAM credentials from AWS metadata store.
        Note: This method should be explicitly called after constructing new
              object, as in 'explicit is better than implicit'.
        """
        request = urllib2.Request(
            urlparse.urljoin(
                urlparse.urljoin(
                    "http://169.254.169.254/",
                    "latest/meta-data/iam/security-credentials/",
                ), self.iamrole))

        response = None
        try:
            response = urllib2.urlopen(request)
            data = json.loads(response.read())
        finally:
            if response:
                response.close()

        self.access_key = data['AccessKeyId']
        self.secret_key = data['SecretAccessKey']
        self.token = data['Token']

    def set_credentials(self, access_key, secret_key):
        self.access_key = access_key
        self.secret_key = secret_key
        self.token = None

    def get_delegated_role_credentials(self, delegated_role):
        """Collect temporary credentials from AWS STS service. Uses
        delegated_role value from configuration.
        Note: This method should be explicitly called after constructing new
              object, as in 'explicit is better than implicit'.
        """
        import boto.sts

        sts_conn = boto.sts.connect_to_region(self.region)
        assumed_role = sts_conn.assume_role(delegated_role, 'yum')

        self.access_key = assumed_role.credentials.access_key
        self.secret_key = assumed_role.credentials.secret_key
        self.token = assumed_role.credentials.session_token

    def get_instance_region(self):
        """Read region from AWS metadata store."""
        request = urllib2.Request(
            urlparse.urljoin(
                "http://169.254.169.254",
                "/latest/meta-data/placement/availability-zone"
            ))

        response = None
        try:
            response = urllib2.urlopen(request)
            data = response.read()
        finally:
            if response:
                response.close()
        self.region = data[:-1]

    def _request(self, path):
        url = urlparse.urljoin(self.baseurl, urllib2.quote(path))
        request = urllib2.Request(url)
        autorization_header = self.signv4(request)
        request.add_header('Authorization', autorization_header)
        return request

    def urlgrab(self, url, filename=None, **kwargs):
        """urlgrab(url) copy the file to the local filesystem."""
        request = self._request(url)
        if filename is None:
            filename = request.get_selector()
            if filename.startswith('/'):
                filename = filename[1:]

        response = None
        try:
            out = open(filename, 'w+')
            response = urllib2.urlopen(request)
            buff = response.read(8192)
            while buff:
                out.write(buff)
                buff = response.read(8192)
        except urllib2.HTTPError, e:
            # Wrap exception as URLGrabError so that YumRepository catches it
            from urlgrabber.grabber import URLGrabError
            new_e = URLGrabError(14, '%s on %s' % (e, url))
            new_e.code = e.code
            new_e.exception = e
            new_e.url = url
            raise new_e
        finally:
            if response:
                response.close()
            out.close()

        return filename

    def urlopen(self, url, **kwargs):
        """urlopen(url) open the remote file and return a file object."""
        return urllib2.urlopen(self._request(url))

    def urlread(self, url, limit=None, **kwargs):
        """urlread(url) return the contents of the file as a string."""
        return urllib2.urlopen(self._request(url)).read()

    def sign(self, request, timeval=None):
        """Attach a valid S3 signature to request.
        request - instance of Request
        """
        date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", timeval or time.gmtime())
        request.add_header('Date', date)
        host = request.get_host()

        # TODO: bucket name finding is ugly, I should find a way to support
        # both naming conventions: http://bucket.s3.amazonaws.com/ and
        # http://s3.amazonaws.com/bucket/
        try:
            pos = host.find(".s3")
            assert pos != -1
            bucket = host[:pos]
        except AssertionError:
            raise yum.plugins.PluginYumExit(
                "s3iam: baseurl hostname should be in format: "
                "'<bucket>.s3<aws-region>.amazonaws.com'; "
                "found '%s'" % host)

        resource = "/%s%s" % (bucket, request.get_selector(), )
        if self.token:
            amz_headers = 'x-amz-security-token:%s\n' % self.token
        else:
            amz_headers = ''
        sigstring = ("%(method)s\n\n\n%(date)s\n"
                     "%(canon_amzn_headers)s%(canon_amzn_resource)s") % ({
                         'method': request.get_method(),
                         'date': request.headers.get('Date'),
                         'canon_amzn_headers': amz_headers,
                         'canon_amzn_resource': resource})
        digest = hmac.new(
            str(self.secret_key),
            str(sigstring),
            hashlib.sha1).digest()
        signature = digest.encode('base64')
        return signature.strip()

    def signv4(self, request, timeval=None):
        """Attach a valid S3 signature to request using AWS Signature 4.
        request - instance of Request
        """

        host = request.get_host()

        # TODO: bucket name finding is ugly, I should find a way to support
        # both naming conventions: http://bucket.s3.amazonaws.com/ and
        # http://s3.amazonaws.com/bucket/
        try:
            pos = host.find(".s3")
            assert pos != -1
            bucket = host[:pos]
            pos_r = host.find(".amazonaws.com")
            assert pos_r != -1
            region_name = host[pos:pos_r].replace(".s3","").replace(".","",1).replace("-","",1)
        except AssertionError:
            raise yum.plugins.PluginYumExit(
                "s3iam: baseurl hostname should be in format: "
                "'<bucket>.s3.<aws-region>.amazonaws.com'; "
                "found '%s'" % host)

        # Variables
        # Equal to h=hashlib.sha256() & h.update('') & h.hexdigest()
        empty_body_sha256 = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'

        service_name = 's3'
        scheme = 'AWS4'
        algorithm = 'HMAC-SHA256'
        terminator = 'aws4_request'

        iso8601basicformat = "%Y%m%dT%H%M%SZ"
        datestringformat = '%Y%m%d'
        # Query string is empty
        #qs = ''
        # Variables

        # Headers & Header Names
        amz_headers = []
        amz_header_names = []

        request.add_header('host', host)
        amz_headers.append('host:%s' % host)
        amz_header_names.append('host')

        request.add_header('x-amz-content-sha256', empty_body_sha256)
        amz_headers.append('x-amz-content-sha256:%s' % empty_body_sha256)
        amz_header_names.append('x-amz-content-sha256')

        current = timeval or time.gmtime()
        date_iso = time.strftime(iso8601basicformat, current)
        date_stamp = time.strftime(datestringformat, current)
        request.add_header('x-amz-date', date_iso)
        amz_headers.append('x-amz-date:%s' % date_iso)
        amz_header_names.append('x-amz-date')

        if self.token:
            request.add_header('x-amz-security-token', self.token)
            amz_headers.append('x-amz-security-token:%s' % self.token)
            amz_header_names.append('x-amz-security-token')

        headers = '\n'.join(amz_headers)
        header_names = ';'.join(amz_header_names)
        # Headers & Header Names

        # Prepare canonical request
        canon_req = self.get_canonical_request(request, ("%s\n" % headers), headers_names, empty_body_sha256)

        # Prepare scope & string to sign
        scope = ("%(date)s/%(region)s/%(service)s/%(terminator)s" % ({'date': date_stamp, 'region': region_name, 'service': service_name, 'terminator': terminator}))
        sigstring = self.get_string_to_sign(date_iso, scope, canon_req)

        # Prepare keys
        data_key = self.sign_msg(("%(scheme)s%(secret_key)s" % ({'scheme': scheme, 'secret_key': self.secret_key})).encode('utf-8'), date_stamp)
        data_region_key = self.sign_msg(data_key, region_name)
        data_region_service_key = self.sign_msg(data_region_key, service_name)
        signing_key = self.sign_msg(data_region_service_key, terminator)

        # Create signature
        signature = self.sign_msg(signing_key, sigstring, hex=True)

        # Prepare autorization header params
        credential_authorization = ("Credential=%(access_key)s/%(scope)s" % ({'access_key': self.access_key, 'scope': scope}))
        signed_headers_authorization = ("SignedHeaders=%(headers_names)s" % ({'headers_names': headers_names}))
        signature_headers_authorization = ("Signature=%(signature)s" % ({'signature': signature}))

        autorization_header = ("%(scheme)s-%(algorithm)s %(credential_authorization)s, %(signed_headers_authorization)s, %(signature_headers_authorization)s" % ({'scheme': scheme, 'algorithm': algorithm, 'credential_authorization': credential_authorization, 'signed_headers_authorization': signed_headers_authorization, 'signature_headers_authorization': signature_headers_authorization}))
        return autorization_header;


    def get_canonical_request(self, request, headers, header_names, body_hash):
        canon_request = ("%(method)s\n%(path)s\n%(params)s\n%(headers)s\n%(header_names)s\n%(body_hash)s" % ({'method': request.get_method(), 'path': request.get_selector(), 'params': '', 'headers': headers, 'header_names': header_names, 'body_hash': body_hash}))
        return canon_request


    def get_string_to_sign(self, date_time, scope, canon_request, scheme='AWS4', algorithm='HMAC-SHA256'):
        h = hashlib.sha256()
        h.update(canon_request)
        sigstring = ("%(scheme)s-%(algorithm)s\n%(datetime)s\n%(scope)s\n%(canonical_request)s" % ({'scheme': scheme, 'algorithm': algorithm, 'datetime': date_time, 'scope': scope, 'canonical_request': h.hexdigest()}))
        return sigstring


    def sign_msg(self, key, msg, hex=False):
        if hex:
          sig = hmac.new(key, msg.encode('utf-8'), hashlib.sha256).hexdigest()
        else:
          sig = hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()
        return sig
