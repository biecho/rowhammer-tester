#!/bin/bash

# Check if a submodule path was provided
if [ -z "$1" ]
then
  echo "Usage: $0 <submodule_path>"
  exit 1
fi

# Assign the submodule path from the first argument
SUBMODULE_PATH=$1

# Ensure the submodule path does not end with a slash
SUBMODULE_PATH=${SUBMODULE_PATH%/}

# Remove the cached reference to the submodule
git rm --cached $SUBMODULE_PATH

# Remove the .git directory within the submodule to prevent it from being a separate Git repository
rm -rf $SUBMODULE_PATH/.git

# Add the submodule files to the staging area, forcing inclusion of ignored files
git add --force $SUBMODULE_PATH

# Commit the changes to the repository
git commit -m "Remove submodule and integrate its contents into main repository"
