from __future__ import unicode_literals

import logging
import sys
import types
import warnings

from django import http
from django.conf import settings
from django.core import signals, urlresolvers
from django.core.exceptions import (
    MiddlewareNotUsed, PermissionDenied, SuspiciousOperation,
)
from django.db import connections, transaction
from django.http.multipartparser import MultiPartParserError
from django.utils import six
from django.utils.deprecation import RemovedInDjango20Warning
from django.utils.encoding import force_text
from django.utils.module_loading import import_string
from django.views import debug

logger = logging.getLogger('django.request')


class BaseHandler(object):
    # Changes that are always applied to a response (in this order).
    response_fixes = [
        # response过滤，去除1xx，204，304以及request.method==HEAD的response内容
        http.conditional_content_removal,
    ]

    def __init__(self):
        # 中间件
        self._request_middleware = None
        self._view_middleware = None
        self._template_response_middleware = None
        self._response_middleware = None
        self._exception_middleware = None

    def load_middleware(self):
        """
        Populate middleware lists from settings.MIDDLEWARE_CLASSES.

        Must be called after the environment is fixed (see __call__ in subclasses).
        """
        self._view_middleware = []
        self._template_response_middleware = []
        self._response_middleware = []
        self._exception_middleware = []

        request_middleware = []
        # 从setting.MIDDLEWARE_CLASSES中加载中间件
        for middleware_path in settings.MIDDLEWARE_CLASSES:
            # 注册middleware，作为module的attr返回给mw_class
            mw_class = import_string(middleware_path)
            try:
                mw_instance = mw_class()
            except MiddlewareNotUsed as exc:
                if settings.DEBUG:
                    if six.text_type(exc):
                        logger.debug('MiddlewareNotUsed(%r): %s', middleware_path, exc)
                    else:
                        logger.debug('MiddlewareNotUsed: %r', middleware_path)
                continue
            # 判断middleware是否有以下属性，分别加入到对应类别的middleware list中
            # request和view中间件采用尾部添加是因为django规定，中间件调用的次序是出现的顺序
            if hasattr(mw_instance, 'process_request'):
                request_middleware.append(mw_instance.process_request)
            if hasattr(mw_instance, 'process_view'):
                self._view_middleware.append(mw_instance.process_view)
            # template，response，exception采用头插法是因为django规定这些中间件调用的次序是逆序
            if hasattr(mw_instance, 'process_template_response'):
                self._template_response_middleware.insert(0, mw_instance.process_template_response)
            if hasattr(mw_instance, 'process_response'):
                self._response_middleware.insert(0, mw_instance.process_response)
            if hasattr(mw_instance, 'process_exception'):
                self._exception_middleware.insert(0, mw_instance.process_exception)

        # We only assign to this when initialization is complete as it is used
        # as a flag for initialization being complete.
        # 单独取一个request_middleware来判断是否已经初始化完成中间件
        self._request_middleware = request_middleware

    def make_view_atomic(self, view):
        non_atomic_requests = getattr(view, '_non_atomic_requests', set())
        for db in connections.all():
            if (db.settings_dict['ATOMIC_REQUESTS']
                    and db.alias not in non_atomic_requests):
                view = transaction.atomic(using=db.alias)(view)
        return view

    # 获取异常response
    def get_exception_response(self, request, resolver, status_code, exception):
        try:
            callback, param_dict = resolver.resolve_error_handler(status_code)
            # Unfortunately, inspect.getargspec result is not trustable enough
            # depending on the callback wrapping in decorators (frequent for handlers).
            # Falling back on try/except:
            try:
                response = callback(request, **dict(param_dict, exception=exception))
            except TypeError:
                warnings.warn(
                    "Error handlers should accept an exception parameter. Update "
                    "your code as this parameter will be required in Django 2.0",
                    RemovedInDjango20Warning, stacklevel=2
                )
                response = callback(request, **param_dict)
        except:
            signals.got_request_exception.send(sender=self.__class__, request=request)
            response = self.handle_uncaught_exception(request, resolver, sys.exc_info())

        return response

    # 返回response
    def get_response(self, request):
        "Returns an HttpResponse object for the given HttpRequest"

        # 为该线程设置一个默认的url处理器，异常会在url处理器设置之前产生故不用try/except包围
        # Setup default url resolver for this thread, this code is outside
        # the try/except so we don't get a spurious "unbound local
        # variable" exception in the event an exception is raised before
        # resolver is set

        # 默认是projectname/urls.py
        urlconf = settings.ROOT_URLCONF
        urlresolvers.set_urlconf(urlconf)
        resolver = urlresolvers.get_resolver(urlconf)
        # Use a flag to check if the response was rendered to prevent
        # multiple renderings or to force rendering if necessary.
        # 防止多个render或暴力render
        response_is_rendered = False
        try:
            response = None
            # Apply request middleware
            for middleware_method in self._request_middleware:
                response = middleware_method(request)
                # request_middleware 采用尾部添加的原因
                if response:
                    break

            # 无request中间件的情况
            if response is None:
                if hasattr(request, 'urlconf'):
                    # Reset url resolver with a custom URLconf.
                    # request中有urlconf时重设url处理器
                    urlconf = request.urlconf
                    urlresolvers.set_urlconf(urlconf)
                    resolver = urlresolvers.get_resolver(urlconf)

                # 匹配request.path_info中的url返回callback以及参数
                resolver_match = resolver.resolve(request.path_info)
                callback, callback_args, callback_kwargs = resolver_match
                request.resolver_match = resolver_match

                # Apply view middleware
                for middleware_method in self._view_middleware:
                    response = middleware_method(request, callback, callback_args, callback_kwargs)
                    if response:
                        break

            if response is None:
                wrapped_callback = self.make_view_atomic(callback)
                try:
                    response = wrapped_callback(request, *callback_args, **callback_kwargs)
                except Exception as e:
                    response = self.process_exception_by_middleware(e, request)

            # 执行到此处可以认定出现错误
            # Complain if the view returned None (a common error).
            if response is None:
                if isinstance(callback, types.FunctionType):    # FBV
                    view_name = callback.__name__
                else:                                           # CBV
                    view_name = callback.__class__.__name__ + '.__call__'
                raise ValueError("The view %s.%s didn't return an HttpResponse object. It returned None instead."
                                 % (callback.__module__, view_name))

            # If the response supports deferred rendering, apply template
            # response middleware and then render the response
            # 如果response支持延迟渲染，加载response中间件然后渲染response
            if hasattr(response, 'render') and callable(response.render):
                for middleware_method in self._template_response_middleware:
                    response = middleware_method(request, response)
                    # Complain if the template response middleware returned None (a common error).
                    if response is None:
                        raise ValueError(
                            "%s.process_template_response didn't return an "
                            "HttpResponse object. It returned None instead."
                            % (middleware_method.__self__.__class__.__name__))
                try:
                    response = response.render()
                except Exception as e:
                    response = self.process_exception_by_middleware(e, request)

                response_is_rendered = True

        # 各类异常及处理方法
        # 404
        except http.Http404 as exc:
            logger.warning('Not Found: %s', request.path,
                        extra={
                            'status_code': 404,
                            'request': request
                        })
            # debug下返回debug的404页面
            if settings.DEBUG:
                response = debug.technical_404_response(request, exc)
            else:
                # 非debug情况下根据路由返回404页面
                response = self.get_exception_response(request, resolver, 404, exc)

        # 权限不足，通常是csrf
        except PermissionDenied as exc:
            logger.warning(
                'Forbidden (Permission denied): %s', request.path,
                extra={
                    'status_code': 403,
                    'request': request
                })
            response = self.get_exception_response(request, resolver, 403, exc)
        # 多个匹配url
        except MultiPartParserError as exc:
            logger.warning(
                'Bad request (Unable to parse request body): %s', request.path,
                extra={
                    'status_code': 400,
                    'request': request
                })
            response = self.get_exception_response(request, resolver, 400, exc)

        # 可疑操作，安全方面
        except SuspiciousOperation as exc:
            # The request logger receives events for any problematic request
            # The security logger receives events for all SuspiciousOperations
            security_logger = logging.getLogger('django.security.%s' %
                            exc.__class__.__name__)
            security_logger.error(
                force_text(exc),
                extra={
                    'status_code': 400,
                    'request': request
                })
            if settings.DEBUG:
                return debug.technical_500_response(request, *sys.exc_info(), status_code=400)

            response = self.get_exception_response(request, resolver, 400, exc)

        except SystemExit:
            # Allow sys.exit() to actually exit. See tickets #1023 and #4701
            raise

        except:  # Handle everything else.
            # Get the exception info now, in case another exception is thrown later.
            signals.got_request_exception.send(sender=self.__class__, request=request)
            response = self.handle_uncaught_exception(request, resolver, sys.exc_info())

        try:
            # Apply response middleware, regardless of the response
            for middleware_method in self._response_middleware:
                response = middleware_method(request, response)
                # Complain if the response middleware returned None (a common error).
                if response is None:
                    raise ValueError(
                        "%s.process_response didn't return an "
                        "HttpResponse object. It returned None instead."
                        % (middleware_method.__self__.__class__.__name__))
            response = self.apply_response_fixes(request, response)
        except:  # Any exception should be gathered and handled
            signals.got_request_exception.send(sender=self.__class__, request=request)
            response = self.handle_uncaught_exception(request, resolver, sys.exc_info())

        response._closable_objects.append(request)

        # If the exception handler returns a TemplateResponse that has not
        # been rendered, force it to be rendered.
        # 异常处理返回一个TemplateResponse，暴力渲染返回
        if not response_is_rendered and callable(getattr(response, 'render', None)):
            response = response.render()

        return response

    # 异常中间件
    def process_exception_by_middleware(self, exception, request):
        """
        Pass the exception to the exception middleware. If no middleware
        return a response for this exception, raise it.
        """
        for middleware_method in self._exception_middleware:
            response = middleware_method(request, exception)
            if response:
                return response
        raise

    # 处理未被捕捉的异常
    def handle_uncaught_exception(self, request, resolver, exc_info):
        """
        Processing for any otherwise uncaught exceptions (those that will
        generate HTTP 500 responses). Can be overridden by subclasses who want
        customised 500 handling.

        Be *very* careful when overriding this because the error could be
        caused by anything, so assuming something like the database is always
        available would be an error.
        """
        if settings.DEBUG_PROPAGATE_EXCEPTIONS:
            raise

        logger.error('Internal Server Error: %s', request.path,
            exc_info=exc_info,
            extra={
                'status_code': 500,
                'request': request
            }
        )

        if settings.DEBUG:
            return debug.technical_500_response(request, *exc_info)

        # If Http500 handler is not installed, re-raise last exception
        if resolver.urlconf_module is None:
            six.reraise(*exc_info)
        # Return an HttpResponse that displays a friendly error message.
        callback, param_dict = resolver.resolve_error_handler(500)
        return callback(request, **param_dict)

    # 对response进行过滤
    def apply_response_fixes(self, request, response):
        """
        Applies each of the functions in self.response_fixes to the request and
        response, modifying the response in the process. Returns the new
        response.
        """
        for func in self.response_fixes:
            response = func(request, response)
        return response


"""
包含：
- BaseHandler类，用来处理httpRequest
- 初始化和加载中间件，用了小技巧判断中间件是否都加载完毕
- 异常处理函数
- response过滤1xx，204, 304和请求方法为HEAD的response内容,
  其中1xx为中间状态，response不应该返回，204为页面内容无更改，304为已存在缓存内容并可以用，请求方法为HEAD不应该返回请求头
- 重要函数get_response()，用url处理器来处理request对象调用中间件并返回response，同时处理多种异常情况
"""