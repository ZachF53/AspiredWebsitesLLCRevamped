"""Context processors that surface SetupTodo counts on every portal page."""


def todo_count(request):
    """Return {todo_count: N} for the sidebar badge. Defensive — fails
    safely if SetupTodo isn't migrated yet or user isn't authed."""
    if not getattr(request, 'user', None) or not request.user.is_authenticated:
        return {'todo_count': 0}
    try:
        from .todo_models import SetupTodo
        n = SetupTodo.objects.filter(
            user=request.user, status='pending').count()
    except Exception:
        n = 0
    return {'todo_count': n}
