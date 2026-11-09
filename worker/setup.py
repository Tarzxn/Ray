"""Prepare a separate unprivileged command account inside the Codespace."""
import getpass,grp,os,pwd,subprocess

def main():
    if os.name!='posix':raise RuntimeError('The worker runs on a Linux Codespace')
    try:account=pwd.getpwnam('beamtask')
    except KeyError:
        subprocess.check_call(['sudo','-n','useradd','--user-group','--create-home','--shell','/bin/bash','beamtask'])
        account=pwd.getpwnam('beamtask')
    groups={grp.getgrgid(g).gr_name for g in os.getgrouplist('beamtask',account.pw_gid)}
    if account.pw_uid==0 or groups & {'root','sudo','wheel','docker'}:raise RuntimeError('beamtask must be an unprivileged account')
    user=getpass.getuser()
    subprocess.check_call(['sudo','-n','usermod','-a','-G','beamtask',user])
    subprocess.check_call(['sudo','-n','install','-d','-o',user,'-g','beamtask','-m','2770','/workspaces/.beam-projects'])

if __name__=='__main__':main()
