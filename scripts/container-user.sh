#!/usr/bin/env bash
set -euo pipefail

name=${1:?expected user name}
uid=${2:?expected uid}
gid=${3:?expected gid}
home=/tmp/crv-home

[[ $name =~ ^[a-zA-Z_][a-zA-Z0-9_.-]*$ && $uid =~ ^[0-9]+$ && $gid =~ ^[0-9]+$ ]]
group=$(getent group "$gid" || true)
user=$(getent passwd "$uid" || true)
[[ -z $group || ${group%%:*} == "$name" ]]
[[ -z $user || ${user%%:*} == "$name" ]]
if [[ -z $group ]]; then
	! getent group "$name" >/dev/null
	printf '%s:x:%s:\n' "$name" "$gid" >> /etc/group
fi
if [[ -z $user ]]; then
	! getent passwd "$name" >/dev/null
	printf '%s:x:%s:%s:CRV user:%s:/bin/bash\n' "$name" "$uid" "$gid" "$home" >> /etc/passwd
fi
install -d -m 0755 -o "$uid" -g "$gid" "$home"
printf '%s\n' 'export PS1="\[\e[0;32m\][\u@\h \W]\$ \[\e[m\] "' > "$home/.bashrc"
chown "$uid:$gid" "$home/.bashrc"
