package credentials

import (
	"os"
	"runtime"
	"unsafe"

	"golang.org/x/sys/windows"
)

// Validate the same handle we read, without following a final reparse point.
// Windows has no POSIX UID: the process token supplies the expected owner.
func openSecretFile(path, label string, _ int) (*os.File, error) {
	unavailable := &CredentialError{msg: label + " is unavailable"}
	name, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return nil, unavailable
	}
	handle, err := windows.CreateFile(name, windows.GENERIC_READ|windows.READ_CONTROL,
		windows.FILE_SHARE_READ, nil, windows.OPEN_EXISTING,
		windows.FILE_FLAG_OPEN_REPARSE_POINT|windows.FILE_FLAG_BACKUP_SEMANTICS, 0)
	if err != nil {
		return nil, unavailable
	}
	valid := false
	defer func() {
		if !valid {
			windows.CloseHandle(handle)
		}
	}()
	var info windows.ByHandleFileInformation
	kind, err := windows.GetFileType(handle)
	if err != nil || kind != windows.FILE_TYPE_DISK {
		return nil, &CredentialError{msg: label + " must be a regular file"}
	}
	if err := windows.GetFileInformationByHandle(handle, &info); err != nil {
		return nil, unavailable
	}
	if info.FileAttributes&(windows.FILE_ATTRIBUTE_DIRECTORY|windows.FILE_ATTRIBUTE_REPARSE_POINT) != 0 {
		return nil, &CredentialError{msg: label + " must be a regular file, not a reparse point"}
	}
	if info.NumberOfLinks != 1 {
		return nil, &CredentialError{msg: label + " must not be hard-linked"}
	}
	sd, err := windows.GetSecurityInfo(handle, windows.SE_FILE_OBJECT,
		windows.OWNER_SECURITY_INFORMATION|windows.DACL_SECURITY_INFORMATION)
	if err != nil {
		return nil, unavailable
	}
	user, err := windows.GetCurrentProcessToken().GetTokenUser()
	if err != nil {
		return nil, unavailable
	}
	if err := validateWindowsSecurity(sd, user.User.Sid, label); err != nil {
		return nil, err
	}
	file := os.NewFile(uintptr(handle), path)
	if file == nil {
		return nil, unavailable
	}
	valid = true
	return file, nil
}

func validateWindowsSecurity(sd *windows.SECURITY_DESCRIPTOR, user *windows.SID, label string) error {
	// ACE and SID pointers below refer into sd's Go-owned allocation.
	defer runtime.KeepAlive(sd)
	owner, _, err := sd.Owner()
	if err != nil || owner == nil || !windows.EqualSid(owner, user) {
		return &CredentialError{msg: label + " must be owned by the current user"}
	}
	acl, _, err := sd.DACL()
	if err != nil || acl == nil {
		return &CredentialError{msg: label + " must have a restricted DACL"}
	}
	for i := uint32(0); i < uint32(acl.AceCount); i++ {
		var ace *windows.ACCESS_ALLOWED_ACE
		if err := windows.GetAce(acl, i, &ace); err != nil {
			return &CredentialError{msg: label + " permissions are unavailable"}
		}
		if ace.Header.AceFlags&windows.INHERIT_ONLY_ACE != 0 || ace.Header.AceType == windows.ACCESS_DENIED_ACE_TYPE {
			continue
		}
		// Reject unfamiliar grant types rather than guessing at their layout.
		if ace.Header.AceType != windows.ACCESS_ALLOWED_ACE_TYPE {
			return &CredentialError{msg: label + " has unsupported permissions"}
		}
		sid := (*windows.SID)(unsafe.Pointer(&ace.SidStart))
		if !windows.EqualSid(sid, user) && !sid.IsWellKnown(windows.WinLocalSystemSid) && !sid.IsWellKnown(windows.WinBuiltinAdministratorsSid) {
			return &CredentialError{msg: label + " must allow access only to the current user, SYSTEM and Administrators"}
		}
	}
	return nil
}
