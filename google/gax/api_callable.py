# Copyright 2016, Google Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:
#
#     * Redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above
# copyright notice, this list of conditions and the following disclaimer
# in the documentation and/or other materials provided with the
# distribution.
#     * Neither the name of Google Inc. nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

"""Provides function wrappers that implement page streaming and retrying."""

from __future__ import absolute_import, division
import random
import sys
import time

from . import (BackoffSettings, BundleOptions, bundling, CallSettings, config,
               OPTION_INHERIT, PageIterator, RetryOptions)
from .errors import GaxError, RetryError

_MILLIS_PER_SECOND = 1000


def _add_timeout_arg(a_func, timeout):
    """Updates a_func so that it gets called with the timeout as its final arg.

    This converts a callable, a_func, into another callable with an additional
    positional arg.

    Args:
      a_func (callable): a callable to be updated
      timeout (int): to be added to the original callable as it final positional
        arg.


    Returns:
      callable: the original callable updated to the timeout arg
    """

    def inner(*args, **kw):
        """Updates args with the timeout."""
        updated_args = args + (timeout,)
        return a_func(*updated_args, **kw)

    return inner


def _retryable(a_func, retry):
    """Creates a function equivalent to a_func, but that retries on certain
    exceptions.

    Args:
        a_func (callable): A callable.
        retry (RetryOptions): Configures the exceptions upon which the callable
          should retry, and the parameters to the exponential backoff retry
          algorithm.

    Returns:
        A function that will retry on exception.
    """

    delay_mult = retry.backoff_settings.retry_delay_multiplier
    max_delay = (retry.backoff_settings.max_retry_delay_millis /
                 _MILLIS_PER_SECOND)
    timeout_mult = retry.backoff_settings.rpc_timeout_multiplier
    max_timeout = (retry.backoff_settings.max_rpc_timeout_millis /
                   _MILLIS_PER_SECOND)
    total_timeout = (retry.backoff_settings.total_timeout_millis /
                     _MILLIS_PER_SECOND)

    def inner(*args, **kwargs):
        """Equivalent to ``a_func``, but retries upon transient failure.

        Retrying is done through an exponential backoff algorithm configured
        by the options in ``retry``.
        """
        delay = retry.backoff_settings.initial_retry_delay_millis
        timeout = (retry.backoff_settings.initial_rpc_timeout_millis /
                   _MILLIS_PER_SECOND)
        exc = RetryError('Retry total timeout exceeded before any'
                         'response was received')
        now = time.time()
        deadline = now + total_timeout

        while now < deadline:
            try:
                to_call = _add_timeout_arg(a_func, timeout)
                return to_call(*args, **kwargs)

            # pylint: disable=broad-except
            except Exception as exception:
                if config.exc_to_code(exception) not in retry.retry_codes:
                    raise RetryError(
                        'Exception occurred in retry method that was not'
                        ' classified as transient', exception)

                # pylint: disable=redefined-variable-type
                exc = RetryError('Retry total timeout exceeded with exception',
                                 exception)
                to_sleep = random.uniform(0, delay)
                time.sleep(to_sleep / _MILLIS_PER_SECOND)
                now = time.time()
                delay = min(delay * delay_mult, max_delay)
                timeout = min(
                    timeout * timeout_mult, max_timeout, deadline - now)
                continue

        raise exc

    return inner


def _bundleable(a_func, desc, bundler):
    """Creates a function that transforms an API call into a bundling call.

    It transform a_func from an API call that receives the requests and returns
    the response into a callable that receives the same request, and
    returns a :class:`bundling.Event`.

    The returned Event object can be used to obtain the eventual result of the
    bundled call.

    Args:
        a_func (callable[[req], resp]): an API call that supports bundling.
        desc (gax.BundleDescriptor): describes the bundling that a_func
          supports.
        bundler (gax.bundling.Executor): orchestrates bundling.

    Returns:
        callable: takes the API call's request and keyword args and returns a
          bundling.Event object.

    """
    def inner(*args, **kwargs):
        """Schedules execution of a bundling task."""
        request = args[0]
        the_id = bundling.compute_bundle_id(
            request, desc.request_discriminator_fields)
        return bundler.schedule(a_func, the_id, desc, request, kwargs)

    return inner


def _page_streamable(a_func, page_descriptor, page_token=None,
                     flatten_pages=True):
    """Creates a function that yields an iterable to performs page-streaming.

    Args:
        a_func (callable[[req], resp]): an API call that is page streaming.
        page_descriptor (:class:`PageDescriptor`): indicates the structure
          of page streaming to be performed.
        page_token (str): Optional. If set and page streaming is over pages of
          the response, indicates the page_token to be passed to the API call.
        flatten_pages (bool): Optional. If set, the returned iterable is over
          ``resource_field``; otherwise the returned iterable is over the pages
          of the response, each of which is an iterable over ``resource_field``.

    Returns:
        A function that returns an iterable.
    """

    def flattened(*args, **kwargs):
        """A generator that yields all the paged responses."""
        request = args[0]
        while True:
            response = a_func(request, **kwargs)
            for obj in getattr(response, page_descriptor.resource_field):
                yield obj
            next_page_token = getattr(
                response, page_descriptor.response_page_token_field)
            if not next_page_token:
                break
            setattr(request,
                    page_descriptor.request_page_token_field,
                    next_page_token)

    def unflattened(*args, **kwargs):
        """A generator that yields individual pages."""
        request = args[0]
        return PageIterator(
            a_func, page_descriptor, page_token, request, **kwargs)

    return flattened if flatten_pages else unflattened


def _construct_bundling(method_config, method_bundling_override,
                        bundle_descriptor):
    """Helper for ``construct_settings()``.

    Args:
      method_config: A dictionary representing a single ``methods`` entry of the
        standard API client config file. (See ``construct_settings()`` for
        information on this yaml.)
      method_retry_override: A BundleOptions object, OPTION_INHERIT, or None.
        If set to OPTION_INHERIT, the retry settings are derived from method
        config. Otherwise, this parameter overrides ``method_config``.
      bundle_descriptor: A BundleDescriptor object describing the structure of
        bundling for this method. If not set, this method will not bundle.

    Returns:
      A tuple (bundling.Executor, BundleDescriptor) that configures bundling.
      The bundling.Executor may be None if this method should not bundle.
    """
    if 'bundling' in method_config and bundle_descriptor:

        if method_bundling_override == OPTION_INHERIT:
            params = method_config['bundling']
            bundler = bundling.Executor(BundleOptions(
                element_count_threshold=params.get(
                    'element_count_threshold', 0),
                element_count_limit=params.get('element_count_limit', 0),
                request_byte_threshold=params.get('request_byte_threshold', 0),
                request_byte_limit=params.get('request_byte_limit', 0),
                delay_threshold=params.get('delay_threshold_millis', 0)))
        elif method_bundling_override:
            bundler = bundling.Executor(method_bundling_override)
        else:
            bundler = None

    else:
        bundler = None

    return bundler


def _construct_retry(
        method_config, method_retry_override, retry_codes, retry_params,
        retry_names):
    """Helper for ``construct_settings()``.

    Args:
      method_config: A dictionary representing a single ``methods`` entry of the
        standard API client config file. (See ``construct_settings()`` for
        information on this yaml.)
      method_retry_override: A RetryOptions object, OPTION_INHERIT, or None.
        If set to OPTION_INHERIT, the retry settings are derived from method
        config. Otherwise, this parameter overrides ``method_config``.
      retry_codes_def: A dictionary parsed from the ``retry_codes_def`` entry
        of the standard API client config file. (See ``construct_settings()``
        for information on this yaml.)
      retry_params: A dictionary parsed from the ``retry_params`` entry
        of the standard API client config file. (See ``construct_settings()``
        for information on this yaml.)
      retry_names: A dictionary mapping the string names used in the
        standard API client config file to API response status codes.

    Returns:
      A RetryOptions object, or None.
    """
    if method_retry_override != OPTION_INHERIT:
        return method_retry_override

    codes = []
    if retry_codes:
        for codes_name in retry_codes:
            if (codes_name == method_config['retry_codes_name'] and
                    retry_codes[codes_name]):
                codes = [
                    retry_names[name] for name in retry_codes[codes_name]]
                break

    params_struct = None
    if method_config.get('retry_params_name'):
        for params_name in retry_params:
            if params_name == method_config['retry_params_name']:
                params_struct = retry_params[params_name].copy()
                break
        backoff_settings = BackoffSettings(**params_struct)
    else:
        backoff_settings = None

    retry = RetryOptions(retry_codes=codes, backoff_settings=backoff_settings)
    return retry


def _upper_camel_to_lower_under(string):
    if not string:
        return ''
    out = ''
    out += string[0].lower()
    for char in string[1:]:
        if char.isupper():
            out += '_' + char.lower()
        else:
            out += char
    return out


def construct_settings(
        service_name, client_config, bundling_override, retry_override,
        retry_names, timeout, bundle_descriptors=None, page_descriptors=None):
    """Constructs a dictionary mapping method names to CallSettings.

    The ``client_config`` parameter is parsed from a client configuration JSON
    file of the form:

    .. code-block:: json

       {
         "interfaces": {
           "google.fake.v1.ServiceName": {
             "retry_codes": {
               "idempotent": ["UNAVAILABLE", "DEADLINE_EXCEEDED"],
               "non_idempotent": []
             },
             "retry_params": {
               "default": {
                 "initial_retry_delay_millis": 100,
                 "retry_delay_multiplier": 1.2,
                 "max_retry_delay_millis": 1000,
                 "initial_rpc_timeout_millis": 2000,
                 "rpc_timeout_multiplier": 1.5,
                 "max_rpc_timeout_millis": 30000,
                 "total_timeout_millis": 45000
               }
             },
             "methods": {
               "CreateFoo": {
                 "retry_codes_name": "idempotent",
                 "retry_params_name": "default"
               },
               "Publish": {
                 "retry_codes_name": "non_idempotent",
                 "retry_params_name": "default",
                 "bundling": {
                   "element_count_threshold": 40,
                   "element_count_limit": 200,
                   "request_byte_threshold": 90000,
                   "request_byte_limit": 100000,
                   "delay_threshold_millis": 100
                 }
               }
             }
           }
         }
       }

    Args:
      service_name: The fully-qualified name of this service, used as a key into
       the client config file (in the example above, this value should be
       ``google.fake.v1.ServiceName``).
      client_config: A dictionary parsed from the standard API client config
       file.
      bundle_descriptors: A dictionary of method names to BundleDescriptor
       objects for methods that are bundling-enabled.
      page_descriptors: A dictionary of method names to PageDescriptor objects
       for methods that are page streaming-enabled.
      bundling_override: A dictionary of method names to BundleOptions
        override those specified in ``client_config``.
      retry_override: A dictionary of method names to RetryOptions that
        override those specified in ``client_config``.
      retry_names: A dictionary mapping the strings referring to response status
        codes to the Python objects representing those codes.
      timeout: The timeout parameter for all API calls in this dictionary.

    Raises:
      KeyError: If the configuration for the service in question cannot be
        located in the provided ``client_config``.
    """
    # pylint: disable=too-many-locals
    defaults = dict()
    bundle_descriptors = bundle_descriptors or {}
    page_descriptors = page_descriptors or {}

    try:
        service_config = client_config['interfaces'][service_name]
    except KeyError:
        raise KeyError('Client configuration not found for service: {}'
                       .format(service_name))

    for method in service_config.get('methods'):
        method_config = service_config['methods'][method]
        snake_name = _upper_camel_to_lower_under(method)

        bundle_descriptor = bundle_descriptors.get(snake_name)
        bundler = _construct_bundling(
            method_config, bundling_override.get(snake_name, OPTION_INHERIT),
            bundle_descriptor)

        retry = _construct_retry(
            method_config, retry_override.get(snake_name, OPTION_INHERIT),
            service_config['retry_codes'], service_config['retry_params'],
            retry_names)

        defaults[snake_name] = CallSettings(
            timeout=timeout, retry=retry,
            page_descriptor=page_descriptors.get(snake_name),
            bundler=bundler, bundle_descriptor=bundle_descriptor)

    return defaults


def _catch_errors(a_func, errors):
    """Updates a_func to wrap exceptions with GaxError

    Args:
        a_func (callable): A callable.
        retry (list[Exception]): Configures the exceptions to wrap.

    Returns:
        A function that will wrap certain exceptions with GaxError
    """
    def inner(*args, **kwargs):
        """Wraps specified exceptions"""
        try:
            return a_func(*args, **kwargs)
        # pylint: disable=catching-non-exception
        except tuple(errors) as exception:
            raise (GaxError('RPC failed', cause=exception), None,
                   sys.exc_info()[2])

    return inner


def create_api_call(func, settings):
    """Converts an rpc call into an API call governed by the settings.

    In typical usage, ``func`` will be a callable used to make an rpc request.
    This will mostly likely be a bound method from a request stub used to make
    an rpc call.

    The result is created by applying a series of function decorators defined
    in this module to ``func``.  ``settings`` is used to determine which
    function decorators to apply.

    The result is another callable which for most values of ``settings`` has
    has the same signature as the original. Only when ``settings`` configures
    bundling does the signature change.

    Args:
      func (callable[[object], object]): is used to make a bare rpc call
      settings (:class:`CallSettings`): provides the settings for this call

    Returns:
      func (callable[[object], object]): a bound method on a request stub used
        to make an rpc call

    Raises:
       ValueError: if ``settings`` has incompatible values, e.g, if bundling
         and page_streaming are both configured

    """
    if settings.retry and settings.retry.retry_codes:
        api_call = _retryable(func, settings.retry)
    else:
        api_call = _add_timeout_arg(func, settings.timeout)

    if settings.page_descriptor:
        if settings.bundler and settings.bundle_descriptor:
            raise ValueError('The API call has incompatible settings: '
                             'bundling and page streaming')
        if settings.flatten_pages is None:
            flatten_pages = True
        else:
            flatten_pages = settings.flatten_pages
        return _page_streamable(
            api_call,
            settings.page_descriptor,
            page_token=settings.page_token,
            flatten_pages=flatten_pages)

    if settings.bundler and settings.bundle_descriptor:
        return _bundleable(api_call, settings.bundle_descriptor,
                           settings.bundler)

    return _catch_errors(api_call, config.API_ERRORS)
