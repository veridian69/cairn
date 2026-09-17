//go:build !windows

package credentials

import (
	"os"
	"syscall"
)

func openSecretFile(path, label string, expectedUID int) (*os.File, error) {
	fd, err := syscall.Open(path, syscall.O_RDONLY|syscall.O_CLOEXEC|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, &CredentialError{msg: label + " is unavailable"}
	}

	file := os.NewFile(uintptr(fd), path)
	if file == nil {
		_ = syscall.Close(fd)
		return nil, &CredentialError{msg: label + " is unavailable"}
	}
	valid := false
	defer func() {
		if !valid {
			file.Close()
		}
	}()

	var stat syscall.Stat_t
	if err := syscall.Fstat(fd, &stat); err != nil {
		return nil, &CredentialError{msg: label + " is unavailable"}
	}
	if stat.Mode&syscall.S_IFMT != syscall.S_IFREG {
		return nil, &CredentialError{msg: label + " must be a regular file"}
	}
	if int(stat.Uid) != expectedUID {
		return nil, &CredentialError{msg: label + " must be owned by the current user"}
	}
	if stat.Mode&0o777 != 0o600 {
		return nil, &CredentialError{msg: label + " must have mode 0600"}
	}
	valid = true
	return file, nil
}
