# bash completion for must-gather-sos
#
# Install:  cp must-gather-sos.bash /usr/share/bash-completion/completions/must-gather-sos
#    or:    source /path/to/must-gather-sos.bash

_must_gather_sos_names() {
	must-gather-sos --list-plugin-names 2>/dev/null
}

_must_gather_sos_option_keys() {
	must-gather-sos --list-plugin-options 2>/dev/null
}

# Complete a comma separated list, keeping everything before the last comma.
_must_gather_sos_comma_list() {
	local cur="$1" candidates="$2"
	local prefix="" last="$cur"
	if [[ $cur == *,* ]]; then
		prefix="${cur%,*},"
		last="${cur##*,}"
	fi
	local word
	COMPREPLY=()
	while IFS= read -r word; do
		[[ -z $word || $word != "$last"* ]] && continue
		COMPREPLY+=("${prefix}${word}")
	done <<<"$candidates"
	compopt -o nospace 2>/dev/null
}

_must_gather_sos() {
	local cur prev
	cur="${COMP_WORDS[COMP_CWORD]}"
	prev="${COMP_WORDS[COMP_CWORD - 1]}"

	case "$prev" in
	-o | --only-plugins | -n | --skip-plugins | -e | --enable-plugins)
		_must_gather_sos_comma_list "$cur" "$(_must_gather_sos_names)"
		return
		;;
	-k | --plugin-option)
		mapfile -t COMPREPLY < <(compgen -W "$(_must_gather_sos_option_keys)" -- "$cur")
		compopt -o nospace 2>/dev/null
		return
		;;
	--format)
		mapfile -t COMPREPLY < <(compgen -W "text json" -- "$cur")
		return
		;;
	--archive)
		mapfile -t COMPREPLY < <(compgen -W "xz gz none" -- "$cur")
		return
		;;
	--reduce-logs)
		_must_gather_sos_comma_list "$cur" \
			"$(printf '%s\n' skip_rotated_logs compress_service_logs compress_logs)"
		return
		;;
	--dest-dir | --catalog | --oc | --source-dir)
		mapfile -t COMPREPLY < <(compgen -d -- "$cur")
		return
		;;
	--since)
		mapfile -t COMPREPLY < <(compgen -W "30s 5m 30m 1h 2h 6h 24h" -- "$cur")
		return
		;;
	esac

	mapfile -t COMPREPLY < <(compgen -W "
		--help --version
		-l --list-plugins --format --list-plugin-names --list-plugin-options
		-o --only-plugins -n --skip-plugins -e --enable-plugins
		-a --all-plugins -k --plugin-option
		--since --since-time --reduce-logs
		--dest-dir --case-id --archive --no-manifest
		--image --image-stream --timeout --node-name --node-selector
		--run-namespace --host-network --volume-percentage --source-dir
		--dry-run --batch -q --quiet -v --verbose --catalog --oc
	" -- "$cur")
}

complete -F _must_gather_sos must-gather-sos
