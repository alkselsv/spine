"""Pinned IANA BCP 47 extension registry used by the R1 profile.

The language-subtag registry is stored in ``profile_data.py``.  IANA's
extension registry is a separate, much smaller registry; keeping it as a
separate signed artifact prevents the validator from accepting arbitrary
singleton extensions merely because they satisfy the BCP 47 grammar.
"""

from __future__ import annotations

import hashlib
import json
import base64
import zlib


EXTENSION_REGISTRY_VERSION = "CLDR-48+IANA-2026-09-17"
_PROFILE_ARTIFACT = "c-nPYNw4HMa{e#x>M7v8S8MleU|$RvKKN=h3fx37xh5AWr9uC9Grmw(6=vFbh=`=fAQg;V|9CNvZc=kOwU^)j_ocu5?Pa|D@wb<1tZTg<-#1g~>C5jg|NdY9@jo9vefzin{QH0Z{loj~`<D-|zkPW7I#={6Qd@G3M!)?2j~D$)tb|e~^)?@irktBtiDs4@G?A;QNklVA?X0717o`B{@v=MJ*Ga7ALGbxE<lWSXW3G+XrbPj2K+k_X{uP%Fmxvl6OjL#pt`U=JV%}(VcRdgurGV=xc6&H_`xS4n9<n-3{~iAqN$(Yh{lR@C+HD^a>pbSYy}o&K%;C4!cP75PzUo3E`1<7=6O&cVj@F9P)P6uFHAj<4Vk!p{g(vB3x&A_g9~2$fg!lJX;E!*=5vIG;hPc|CZ7>_Hbj?-TG8iiqZ4~<3`|D>WK7V_MjJgo_N#sWIE~0la@qNFJ2I3@<iQj(v@z$H5Lu~F&Xm?5Hy^$0m-@oar%lxuIgiMzsO`n$zrI41yCx`O82qaSV_C~NZ=mb49?c=vkbUd?TiOr2B+EIlyAi5P5kDS0GXWGr+f6Eiz#73_XM3l@iDol%s=FCec4P^={5(H+ITAs?;rX*-?GnF`X<DC(~K?K%V)&QrnrpXU6MQn)SBL;?VuOyK6{}=>o_YY<Mp;SM0^`}a9@F2m@KM(vV)or4i?_)0i<IkTbnI@<NO^`$D)!~)Z>XN}WDwpo)GPFpRPJw$8;z<=ew9^6Pdf<?<nw?~tv=a%!of5^Yi~6A4=E5)kt8L<!zg>o3|Bfiaebirum{<Px<@F8MR>XDKLf!|P)~P`A&DC;=G7nr`>(i1%g;Z3=OFR2{#S)d{)MTiWVpI(iL*bgHBe+2VnKanS_m6L+Ze`8MNdKf*T0btQn#<FXR*0(BAH*&EdLSZe_J;kVW=-LPo$DZ8bmW28(S@`EJjEga6YRzV5?>MVG+BZfb26T0%)}cA$W%a?MU8;qT%mQWdjouS@HsK(geBsLQba(ENQ~$X!)5bb00ZooVf<;!mTXS0SmiJiXO?glGD|?L$^~#ESTSC~zZyWyso+^FAo(8Iz^Aj{pm&!$_#EJHVEn-R6EOyFr1S#yhw86PlQt{3eibkhh>uDu-~;erQvn<VaYBVNs7AZ1fsuhZ2q@|ReZpUBl8iQhH`+yejDx4f35{xiBnM;Ysj>9bRCa%`$tD3Y-a!v~$Hfun<%V$S>FH7zXfe?i+$?U&t}y5oF4W;E?!_j|hcC2uI8>;u?Gy>@juA-M9yGopu&#-Cu9&%2+GXveOY0zRPFJ{LTIB*mj0AY)1o}>E9d6dO8zACTC(bvV?Un%x-GYZl_ZD!aRX);QzR*4Tx&mfEjO?`5<IMi-2mr)CKr#W5GvNH81w=uHn29?Lc$Y9RzA?lq@Qi}tf`9_36(G%wRsdog5{n+82bvb6NBlt)f+j?qJXX@Nh?B&Qaolp;Kx6l~W9<Xn9vFX+=0xaxJPlw3MB;;UCfH_z4@-{rN`dYx!F!}aYZJ6F$BNi5;ddP%q7md@EIUNnw?Kv^;AtS+EFIqZAkKHB1r>gv!rm)<RE6!-AptRGUl3!7Ztyluf!3h41uSr|w0pA{#~B*(um+oIu;m6n*^odrcEA-JU^}g|OQw@F?A|t#NrTO{lh*c?&em@gkofc%0co!eKh|+;y|7=$Dul@n{vEp4Pg*;~3P>j$(B5Imw+zT(G$ap5cV;vIEPw-$G<kr}W+Wg|E_mP#H?naOmWa<bNE3F1`Uf=gAuJ$qdq@E&0Tm#veJF;Q3Cn#L%wIAf=nQTJGYiw$1-|;xFb9$4F|f;n-toW<9!RSXM4V$pzy!$Yb^rtoyEr4g^$G8H!snlm=ZR=_?wG$<3Y_{ufpe;hEB!mNo<As_)1Wqs7~$9W-e+P=&zGH^FUW=?bh05oXgF10>0SDu_u36Iis?D$0)?zVy?-J%hb(o)$ol>f@DoyhWLzmbn=(YxR=0F!<0bYLp|REsoYt$q99@1LeHJ`iqR`{@FBkd098G^YTJcV!R+CT;JPWWg60)N@(;;o@_baPyMJV7SQuHPcC>5Qa*(}`3tTRk_dlK(Y;^UKG`E38R?Q^?-<sTrfkTa*nGuslJSyjs8XaeqWW(&e|a^#IEO#PT$)+!oaIO4do?5H^H@a<?Fad}!FlQavNm@=WcLE5~Cb)FPygEwL0bS+)!T0q{kW=^r-F$s~HA@fQXn%9SP3KfGiNZ#b?QU6r92PJ&udKl<{N6`j#%S^ZZm6aplCy^kjWmdGaB1U}Xl+RunY{m`~MNZ-Uh4>tHz_Z!mS1zMS-)BbZv^d(6M4~sTYoIMidMDK{bPdd^fKbGa6P2I!)~z6aB*c@6It&cmvn;&WfmXyEKQm^8Z-F{hBypk6VhJK=Ab9U!3bf8ei8wsHHa8WB)G}PuH1K?pO(gk~Y>37O0%g{hFR&bnv7#gR#RF#}WQrFTSwz05#<@I^Jz#wj$cE>!9n!&Mc3PG^*yf<<I&uPsSz8Iz+YxB=FzYjpn5zYfSJ^W$Utbd(wv3Hjwl>bfnoAc#DUzWPjZ5^2;MBNZ&_&>XC{GFTb$7$#E9BpuM7VRvta?Hk+A}gZs$^V88nFq~usdtuuGWmcktGYM_PaO7_igS66Pyu`y!Du6_~xjc^uuMh#%?p2e=6qs$)+a4#<kG{R<?Ltu&@FzCY*Sn<_X;aN3+5Bl7g^A4#<MUT|`trcO@8Wu`$-*bB|(YtYFml!Zv=d88&@y2S4YSFYX)i3{fTG#Vr4>3QTnc?CK5pHW-I-XQ!Y~;A*)_Yk_OFK6u>_Ja3@>7p)XjYJGFqX9#4OV#xK*IPthf?kk32K4V497sDs6`hr#$2Hz14^NDwKXa={y#ApZChXT=DoDx1rigvr9CY0zINUa4|gc4pvE*bNsZO9ko+<rwJC~v3`Z-wi&C}HX{$vtsBSWvy)J=AD=#qr>(kgLopx2rz1Ps|mex$48kTsZT1gn3P@P&3xDtz1V+eqOnft?ioE8t>aW&e;<5Lw0t$a<>vS=@Hu<m8C?Ut%O?czy@`m63fyUCw^{&WDMh1VntL^!rd8j{xNZ{zzO%PM1g55QH{JXk2lniN^q7kaprX!w>rcQ53Ix?AB+{RKOk?yeBJ{=f$+xoaWFoZ&v7mn#{0~l=PFu8#tva?k*ILyt(@Sqp70jR2}ydQ=5^wHK|Qc<aB$kQ(jNT3Dac_KO*mgt-f|6&et=4T<^R*o0uQdgv&<N;?FPBF{zaifp~eb}RvSkV^?0B)qmCtF#lba@It!`-Z6q{);>I<SzTKQeovsS4)KMeA>)aqaJrh=odfgVT+wEacxp$-<#wzS=R>OEEAQ82@LRM{BC|uEV1!-FUY;bnd1@h%7TWI~P?P1-^ts$?U^?jpTTB3)xF0mw}1hJ$3S#n=9IsgSyw<5?5_!JkK$rCDh8K8GoTf_%zx<viI%o`_ct#arV2sGvF%neB<HfY~gxK<l%M*a}8#FD@&X`@5~%%WJb^Fr~oNEn_i5si!~I*HZy){v`IfzG~dw&KX$!jafvQ83s&!|~f=<~Ud0fj3zF3XNIQT!CK1iHc|KMHKWSB<c>QH9VKoH;)#v=El0yB>@|aiqu7IxksTC@pD<Dl6QA$(fzD_<&H%~OW*Yh?YNogssmFEuX2l?vz{zaJx<qmpgUob1hxx&re6ypws-1*WXKiVqMxkM$A|~gDW4>Kk}pfTAx&z~62E&+qNZpRdL07Q-o*aF7`Ek4JHYSss5vJ-vCqjp*dR7k??FnuSx_7!gWC|Rf>+42LXBZZ*Ce9q3w=X|6-I#14qJ7=M@N?_3g$B-DsC)l@6fL>9d&%P9sQuVdgMuQjpBxWNkcy?2I-M(#R~h3N##56x?z19=s?6A7bJ6~75u+1==sH(@I@%L2y2P0e~qxW*drtpM?vBfw}=ix+$Fjhak>u27+rvg2X|d&RtX%Dmrl$b{71y6oQd(dWMcH4;)CnY_$b`Ld9?1BVRv}d!^25UjAm0jW8oBXN0?43q4}6hLc1?52`%9?jjy4h!|kRNIK4@;W<nKIJi=M34rx*v4rxj{`4dt))!NbWiKxM*$>89{1ng55%gvx=${M(!=r6WF>B}_R9Yt0|JFs{Uft9fHc#L;6%~MhQMWx;79HsSd1y@k|k}zW?zGyG?g2blm6@jfB0!nrlI`c=n3MX2#9QfZP0^`h=+QMxm@c^DzhmTivXf(#^u$rGZIDk65<wlfWlV%>Ad`vu;m*03rMB-gX$|rD!lqcS}iB+Irb=1!Drf*=@Ju{;b4#vq`Ta3Xhw#-{^TL$L1JTa=nx_jh@Z6<=q>gDQ?{435=oAwnY!1Os8ov{X=)7G{Ey8wZ=TYwOnm;4<oEGVv~2_r_eq){2P9PJ$)vw}F^=6y+I<Q=cvw#GI1qu`=vmIw=I=N-jvR~FaWb<(8ReJKkn4jY!8J8Otk{if01>VAQ3_n`6Ns7HbD&{OALOzsl%Du2G}y+Gmnw(#@)>cE{E5pM{Gd}fGza@VcF2Msa!zxA;AF~x^|T)e?2jTrTTPcs;8LSGolH8GTQG?~$%YwQ<B(<WUQU(6eK#m7~O5#IPb?FIuJ%_r{t1##iQoMPfmM9jQX?EF8?%nm8eB(+NYqEh3H(!tZS5sdM<a<sBCdv1@8)(y|!;n*fd8avu!@c6;)Nfa#;8hn5Q@%|(}K8a6H;`5XE@+7`KiEmHhw~KgteG*U3Z%@u|PtI=_=l!|+{@i_k?!LeNnDhBbe0dUIpTxH(aqYf8ci*48@4WjF5kkh<JmZc~wN0Uu+&0xVZnr9YqS<W6$EsPp;xoaPn!VbtXlLw8MCjd{z(<M02()L8&Vk|-H*_G*7@)rAzE+gK8@h&5w`b&}za34eSsRz^=v(n0fByM@nE7l-"
_PROFILE_ARTIFACT_DIGEST = "3dce486a77d7efe3dcc58dc44de23f7ab505632157cdb5f4e5d7dadd3a1b898a"


def _load_profile_artifact() -> dict[str, object]:
    raw = zlib.decompress(base64.b85decode(_PROFILE_ARTIFACT))
    if hashlib.sha256(raw).hexdigest() != _PROFILE_ARTIFACT_DIGEST:
        raise RuntimeError("Pinned BCP47 extension profile digest mismatch.")
    return json.loads(raw)


EXTENSION_DATA = _load_profile_artifact()
EXTENSION_TABLE_DIGEST = _PROFILE_ARTIFACT_DIGEST
REGISTERED_EXTENSION_SINGLETONS = frozenset(EXTENSION_DATA["extensions"])
UNICODE_EXTENSION_VALUES = {
    key: frozenset(values) for key, values in EXTENSION_DATA["unicode"].items()
}
TRANSFORMED_EXTENSION_VALUES = {
    key: frozenset(values)
    for key, values in EXTENSION_DATA["transformed"].items()
}


__all__ = [
    "EXTENSION_DATA",
    "EXTENSION_REGISTRY_VERSION",
    "EXTENSION_TABLE_DIGEST",
    "REGISTERED_EXTENSION_SINGLETONS",
    "TRANSFORMED_EXTENSION_VALUES",
    "UNICODE_EXTENSION_VALUES",
]
