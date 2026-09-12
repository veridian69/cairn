package credentials

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/sys/windows"
)

// These tests exercise real Windows file handles and security descriptors.
func TestWindowsSecretFilePermissions(t *testing.T) {
	user, err := windows.GetCurrentProcessToken().GetTokenUser()
	if err != nil {
		t.Fatal(err)
	}
	sid := user.User.Sid.String()
	for _, tc := range []struct {
		name, owner, acl string
		valid            bool
	}{
		{"owner only", sid, "(A;;FA;;;" + sid + ")", true},
		{"system and administrators", sid, "(A;;FA;;;" + sid + ")(A;;FA;;;SY)(A;;FA;;;BA)", true},
		{"everyone read", sid, "(A;;FA;;;" + sid + ")(A;;FR;;;WD)", false},
		{"everyone write", sid, "(A;;FA;;;" + sid + ")(A;;FW;;;WD)", false},
		{"authenticated users", sid, "(A;;FA;;;" + sid + ")(A;;FR;;;AU)", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "secret")
			if err := os.WriteFile(path, []byte("sentinel-secret\r\n"), 0600); err != nil {
				t.Fatal(err)
			}
			sd, err := windows.SecurityDescriptorFromString("O:" + tc.owner + "D:P" + tc.acl)
			if err != nil {
				t.Fatal(err)
			}
			acl, _, err := sd.DACL()
			if err != nil {
				t.Fatal(err)
			}
			err = windows.SetNamedSecurityInfo(path, windows.SE_FILE_OBJECT, windows.DACL_SECURITY_INFORMATION|windows.PROTECTED_DACL_SECURITY_INFORMATION, nil, nil, acl, nil)
			if err != nil {
				t.Fatal(err)
			}
			got, err := ReadSecretFile(path, "test credential", os.Geteuid(), 4096)
			if tc.valid {
				if err != nil || string(got) != "sentinel-secret" {
					t.Fatalf("read = %q, %v", got, err)
				}
			} else if err == nil {
				t.Fatal("accepted a credential accessible to other users")
			}
			if err != nil && strings.Contains(err.Error(), "sentinel-secret") {
				t.Fatal("secret leaked in error")
			}
		})
	}
}

func TestWindowsRejectsUnrestrictedOrForeignSecurityDescriptor(t *testing.T) {
	user, err := windows.GetCurrentProcessToken().GetTokenUser()
	if err != nil {
		t.Fatal(err)
	}
	sid := user.User.Sid.String()
	for _, sddl := range []string{
		"O:" + sid + "D:NO_ACCESS_CONTROL",
		"O:SYD:P(A;;FA;;;" + sid + ")",
	} {
		sd, err := windows.SecurityDescriptorFromString(sddl)
		if err != nil {
			t.Fatal(err)
		}
		if err := validateWindowsSecurity(sd, user.User.Sid, "credential"); err == nil {
			t.Fatal("accepted null DACL or foreign owner")
		}
	}
}

func TestWindowsRejectsNonRegularFiles(t *testing.T) {
	dir := t.TempDir()
	if _, err := ReadSecretFile(dir, "credential", os.Geteuid(), 4096); err == nil {
		t.Fatal("accepted directory")
	}
	target := filepath.Join(dir, "target")
	if err := os.WriteFile(target, []byte("sentinel"), 0600); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(dir, "link")
	if err := os.Symlink(target, link); err != nil {
		t.Skipf("Windows symlink creation requires Developer Mode or privilege: %v", err)
	}
	if _, err := ReadSecretFile(link, "credential", os.Geteuid(), 4096); err == nil {
		t.Fatal("accepted symlink")
	}
}

func TestWindowsRejectsHardLinkedSecret(t *testing.T) {
	dir := t.TempDir()
	target := writeSecret(t, dir, "target", []byte("sentinel-secret"), 0600)
	if got, err := ReadSecretFile(target, "credential", os.Geteuid(), 4096); err != nil || string(got) != "sentinel-secret" {
		t.Fatalf("single-link read = %q, %v", got, err)
	}
	link := filepath.Join(dir, "link")
	if err := os.Link(target, link); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{target, link} {
		if _, err := ReadSecretFile(path, "credential", os.Geteuid(), 4096); err == nil {
			t.Fatal("accepted hard-linked credential")
		} else if strings.Contains(err.Error(), "sentinel-secret") {
			t.Fatal("secret leaked in hardlink error")
		}
	}
	if err := os.Remove(link); err != nil {
		t.Fatal(err)
	}
	if got, err := ReadSecretFile(target, "credential", os.Geteuid(), 4096); err != nil || string(got) != "sentinel-secret" {
		t.Fatalf("restored single-link read = %q, %v", got, err)
	}
}

// Shared behavioural tests must use real owner-restricted Windows files.
func writeSecret(t *testing.T, base, name string, value []byte, mode os.FileMode) string {
	t.Helper()
	path := filepath.Join(base, name)
	if err := os.WriteFile(path, value, mode); err != nil {
		t.Fatal(err)
	}
	user, err := windows.GetCurrentProcessToken().GetTokenUser()
	if err != nil {
		t.Fatal(err)
	}
	sd, err := windows.SecurityDescriptorFromString("D:P(A;;FA;;;" + user.User.Sid.String() + ")")
	if err != nil {
		t.Fatal(err)
	}
	acl, _, err := sd.DACL()
	if err != nil {
		t.Fatal(err)
	}
	if err := windows.SetNamedSecurityInfo(path, windows.SE_FILE_OBJECT, windows.DACL_SECURITY_INFORMATION|windows.PROTECTED_DACL_SECURITY_INFORMATION, nil, nil, acl, nil); err != nil {
		t.Fatal(err)
	}
	return path
}
