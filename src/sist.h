#ifndef SIST_H
#define SIST_H

#define _GNU_SOURCE

#ifndef	FALSE
#define	FALSE	(0)
#define BOOL int
#endif

#ifndef	TRUE
#define	TRUE	(!FALSE)
#endif

#undef	MAX
#define MAX(a, b)  (((a) > (b)) ? (a) : (b))

#undef	MIN
#define MIN(a, b)  (((a) < (b)) ? (a) : (b))

#ifndef PATH_MAX
#define PATH_MAX 4096
#endif

#undef ABS
#define ABS(a)	   (((a) < 0) ? -(a) : (a))

#define UNUSED(x) __attribute__((__unused__))  x

#include "util.h"
#include "log.h"
#include "types.h"

#include "libscan/scan.h"

#include <cjson/cJSON.h>

#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>

#include <unistd.h>
#include <fcntl.h>
#include <dirent.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <errno.h>
#include <ctype.h>
#include "git_hash.h"

#define VERSION "2.14.2"
static const char *const Version = VERSION;

#ifndef SIST_PLATFORM
#define SIST_PLATFORM unknown
#endif

#define EXPECTED_MONGOOSE_VERSION "7.7"

#define Q(x) #x
#define QUOTE(x) Q(x)

#endif
