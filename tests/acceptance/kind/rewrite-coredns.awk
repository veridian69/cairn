BEGIN { inserted = 0 }
/^\.:53[[:space:]]*\{/ && !inserted {
    print
    print "    rewrite name exact " source " " target
    inserted = 1
    next
}
{ print }
END { if (!inserted) exit 1 }
