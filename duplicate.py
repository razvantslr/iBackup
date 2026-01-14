from collections import defaultdict

def find_duplicates(media_files):
    # find duplicates
    duplicates = defaultdict(list)
    for media in media_files:
        if media.hash is None:
            continue
        duplicates[media.hash].append(media)
    duplicate_groups = {
        h: files for h, files in duplicates.items()
        if len(files) > 1
    }

    print("\nDuplicates files:")
    if not duplicate_groups:
        print("no dulicates found.")
    else:
        for h, files in duplicate_groups.items():
            print(f"\nHash: {h}")
            for f in files:
                print(f" {f.path}")
