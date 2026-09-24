"""
Custom middleware for admin login rate limiting.
Uses Django's cache framework — gracefully degrades if cache is unavailable.
"""
from django.core.cache import cache
from django.conf import settings
from django.http import JsonResponse


class AdminLoginRateLimiter:
    """
    Rate-limits failed login attempts to /admin/login/.
    Locks out the IP after ADMIN_LOGIN_RATE_LIMIT failures.
    Gracefully degrades if Redis/cache is not available.
    """
    
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Only apply to admin login POST requests
        if request.path == "/admin/login/" and request.method == "POST":
            try:
                return self._check_rate_limit(request)
            except Exception:
                # Cache unavailable — skip rate limiting, allow request through
                pass
        
        return self.get_response(request)

    def _check_rate_limit(self, request):
        ip_address = self._get_client_ip(request)
        cache_key = f"admin_login_failures:{ip_address}"
        
        # Check if IP is locked out
        lockout_key = f"admin_login_lockout:{ip_address}"
        if cache.get(lockout_key):
            return JsonResponse(
                {"error": "Too many failed login attempts. Try again later."},
                status=429
            )
        
        # Process the request
        response = self.get_response(request)
        
        # If response indicates failed login (Django redirects back to login with error)
        if response.status_code == 200 and b"error" in response.content.lower():
            failures = cache.get(cache_key, 0) + 1
            cache.set(cache_key, failures, timeout=300)  # 5 minute window
            
            # Lock out if exceeded
            limit = self._parse_rate_limit(settings.ADMIN_LOGIN_RATE_LIMIT)
            if failures >= limit:
                cache.set(lockout_key, True, timeout=300)
                return JsonResponse(
                    {"error": "Too many failed login attempts. Try again in 5 minutes."},
                    status=429
                )
        else:
            # Successful login — clear failure count
            cache.delete(cache_key)
        
        return response

    def _get_client_ip(self, request):
        x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded_for:
            return x_forwarded_for.split(",")[0].strip()
        return request.META.get("REMOTE_ADDR")

    def _parse_rate_limit(self, rate_str):
        """Parse '5/5m' format into max attempts."""
        try:
            return int(rate_str.split("/")[0])
        except (AttributeError, IndexError, ValueError):
            return 5  # Default fallback