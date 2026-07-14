"""Business logic for the Meetings dashboard (read-only analytics over dakavara_pa)."""


class MeetingsService:
    def __init__(self, repo):
        self.repo = repo

    def filters(self):
        return self.repo.filter_options()

    def overview(self, filters):
        return self.repo.overview(filters)

    def meetings(self, filters, limit=50, offset=0, sort="recent"):
        return self.repo.meetings(filters, limit=limit, offset=offset, sort=sort)
