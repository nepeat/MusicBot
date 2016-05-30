def load(filename, skip_commented_lines=True, comment_char='#'):
    try:
        with open(filename, encoding='utf8') as f:
            results = []
            for line in f:
                line = line.strip()

                if line and not (skip_commented_lines and line.startswith(comment_char)):
                    results.append(line)

            return results

    except IOError as e:
        print("Error loading", filename, e)
        return []


def write(filename, contents):
    with open(filename, 'w', encoding='utf8') as f:
        for item in contents:
            f.write(str(item))
            f.write('\n')
