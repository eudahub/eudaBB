from django.conf import settings


def test_mode(request):
    return {"TEST_MODE": getattr(settings, "TEST_MODE", False)}
