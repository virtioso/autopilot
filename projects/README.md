# projects/

A customer or project drop-in: `projects/<name>/{chains,platforms,placements}`,
placed here by a workspace manifest (`repo`'s `<linkfile>` pointing at that
project's own repository) or checked out by hand. Nothing under it is tracked
here; see model/projects.py for how a bare `--chain` or `--platform` name is
resolved across it, and why a name defined twice is refused.
